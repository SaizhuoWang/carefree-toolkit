import os
import dill
import time
import pprint
import random
import signal
import inspect
import logging
import platform

from typing import *
from pathos.pools import ProcessPool
from multiprocessing import Process
from multiprocessing.managers import SyncManager

from ..misc import *
from ..manage import *

WINDOWS = platform.system() == "Windows"
dill._dill._reverse_typemap["ClassType"] = type


class Parallel(PureLoggingMixin):
    """
    Util class which can help running tasks in parallel

    Warnings
    ----------
    On Windows platform, functions are dramatically reduced because Windows does not well support pickling.
    * In this occasion, `Parallel` will simply leverage `pathos` to do the jobs

    Parameters
    ----------
    num_jobs : int, number of jobs run in parallel
    sleep : float, idle duration of new jobs
    use_cuda: bool, whether tasks need CUDA or not
    name : str, summary name of these tasks
    meta_name : str, name of the meta information
    logging_folder : str, where the logging will be placed
    task_names : List[str], names of each task
    resource_config : Dict[str, Any], config used in `ResourceManager`

    Examples
    ----------
    >>> def add_one(x):
    >>>     import time
    >>>     time.sleep(1)
    >>>     return x + 1
    >>>
    >>> print(Parallel(10)(add_one, list(range(10)))._rs)

    """

    class _ParallelError(Exception):
        pass

    def __init__(self,
                 num_jobs: int,
                 *,
                 sleep: float = 1.,
                 use_cuda: bool = False,
                 name: str = None,
                 meta_name: str = None,
                 logging_folder: str = None,
                 task_names: List[str] = None,
                 resource_config: Dict[str, Any] = None):
        self._rs = None
        self._n_jobs, self._sleep, self._use_cuda = num_jobs, sleep, use_cuda
        if resource_config is None:
            resource_config = {}
        if logging_folder is None:
            logging_folder = os.path.join(os.getcwd(), "_parallel_", "logs")
        self._resource_config = resource_config
        self._name, self._meta_name = name, meta_name
        self._logging_folder, self._task_names = logging_folder, task_names
        self._refresh_patience = resource_config.setdefault("refresh_patience", 10)
        self._init_logger(self.meta_log_name)

    def __call__(self, f, *args_list) -> "Parallel":
        #   if f returns a dict with 'terminate' key, Parallel can be terminated at early stage by
        # setting 'terminate' key to True
        n_tasks = len(args_list[0])
        n_jobs = min(self._n_jobs, n_tasks)
        if WINDOWS:
            p = ProcessPool(ncpus=n_jobs)
            task_names = list(map(self._get_task_name, range(n_tasks)))
            results = p.map(f, *args_list)
            self._rs = dict(zip(task_names, results))
            return self
        self._func, self._args_list = f, args_list
        self._cursor, self._all_task_ids = 0, list(range(n_jobs, n_tasks))
        self._log_meta_msg("initializing sync manager")
        self._sync_manager = SyncManager()
        self._sync_manager.start(lambda: signal.signal(signal.SIGINT, signal.SIG_IGN))
        self._rs = self._sync_manager.dict({
            "__meta__": {"n_jobs": n_jobs, "n_tasks": n_tasks, "terminated": False},
            "__exceptions__": {}
        })
        self._overwritten_task_info = {}
        self._pid2task_id = None
        self._log_meta_msg("initializing resource manager")
        self._resource_manager = ResourceManager(
            self._resource_config, self._get_task_name, self._refresh_patience)
        self._log_meta_msg("registering PC manager")
        pc_manager = PCManager()
        ram_methods = {
            "get_pid_usage_dict": None,
            "get_pid_usage": pc_manager.get_pid_ram_usage,
            "get_available_dict": lambda: {"total": pc_manager.get_available_ram()}
        }
        self._resource_manager.register("RAM", ram_methods)
        gpu_config = self._resource_config.setdefault("gpu_config", {})
        default_cuda_list = None if self._use_cuda else []
        available_cuda_list = gpu_config.setdefault("available_cuda_list", default_cuda_list)
        if available_cuda_list is None or available_cuda_list:
            self._log_meta_msg("registering GPU manager")
            if available_cuda_list is not None:
                available_cuda_list = list(map(int, available_cuda_list))
            gpu_manager = GPUManager(available_cuda_list)
            gpu_methods = {
                "get_pid_usage": None,
                "get_pid_usage_dict": gpu_manager.get_pid_usages,
                "get_available_dict": gpu_manager.get_gpu_frees
            }
            self._resource_manager.register("GPU", gpu_methods)
        self._resource_manager.register_logging(self._init_logger, self)
        self._log_meta_msg("initializing with refreshing")
        self._refresh(skip_check_finished=True)
        self._working_processes = None
        try:
            self._log_meta_msg("initializing processes")
            init_task_ids = list(range(n_jobs))
            init_processes = [self._get_process(i, start=False) for i in init_task_ids]
            if self.terminated:
                self._user_terminate()
            init_failed_slots, init_failed_task_ids = [], []
            for i, (task_id, process) in enumerate(zip(init_task_ids, init_processes)):
                if process is None:
                    init_failed_slots.append(i)
                    init_failed_task_ids.append(task_id)
                    task_name = self._get_task_name(task_id)
                    self._log_with_meta(
                        task_name, "initialization failed, it may due to lack of resources",
                        msg_level=logging.WARNING
                    )
            if init_failed_slots:
                for slot in init_failed_slots:
                    init_task_ids[slot] = None
                    init_processes[slot] = [None] * 4
                self._all_task_ids = init_failed_task_ids + self._all_task_ids
            self._working_task_ids = init_task_ids
            self._working_processes, task_info = map(list, zip(*init_processes))
            self._log_meta_msg("starting all initial processes")
            tuple(map(lambda p: None if p is None else p.start(), self._working_processes))
            tuple(map(self._record_process, self._working_processes, self._working_task_ids, task_info))
            self._resource_manager.initialize_running_usages()
            self._log_meta_msg("entering parallel main loop")
            while True:
                self._log_meta_msg("waiting for finished slot")
                self._wait_and_handle_finish(wait_until_finish=True)
                if not self._add_new_processes():
                    break
        except KeyboardInterrupt:
            self.exception(self.meta_log_name, f"keyboard interrupted")
            exceptions = self.exceptions
            exceptions["base"] = self._ParallelError("Keyboard Interrupted")
            self._rs["__exceptions__"] = exceptions
        except Exception as err:
            self.exception(self.meta_log_name, f"exception occurred, {err}")
            exceptions = self.exceptions
            exceptions["base"] = err
            self._rs["__exceptions__"] = exceptions
        finally:
            self._log_meta_msg("joining processes left behind")
            if self._working_processes is not None:
                for process in self._working_processes:
                    if process is None:
                        continue
                    process.join()
            self._log_meta_msg("casting parallel results to Python dict")
            self._rs = dict(self._rs)
            self._log_meta_msg("shutting down sync manager")
            self._sync_manager.shutdown()
            self.log_block_msg(
                self.meta_log_name, "parallel results",
                pprint.pformat(self._rs, compact=True)
            )
        return self

    @property
    def meta(self):
        return self._rs["__meta__"]

    @property
    def exceptions(self):
        return self._rs["__exceptions__"]

    @property
    def terminated(self):
        return self.meta["terminated"]

    @property
    def parallel_results(self):
        return self._rs

    def __sleep(self, skip_check_finished):
        time.sleep(self._sleep + random.random())
        self._refresh(skip_check_finished=skip_check_finished)

    def __wait(self, wait_until_finished):
        # should return a sorted list
        try:
            while True:
                self._log_meta_msg(
                    "waiting for slots (working tasks : "
                    f"{', '.join(map(self._get_task_name, filter(bool, self._working_task_ids)))})",
                    msg_level=logging.DEBUG
                )
                finished_slots = []
                for i, (task_id, process) in enumerate(zip(
                        self._working_task_ids, self._working_processes)):
                    if process is None:
                        self._log_meta_msg(f"pending on slot {i}")
                        finished_slots.append(i)
                        continue
                    task_name = self._get_task_name(task_id)
                    if not process.is_alive():
                        self._log_with_meta(task_name, f"in slot {i} is found finished")
                        finished_slots.append(i)
                if not wait_until_finished or finished_slots:
                    return finished_slots
                self.__sleep(skip_check_finished=True)
        except KeyboardInterrupt:
            self._set_terminate(scope="wait")
            raise self._ParallelError("Keyboard Interrupted")

    def _init_logger(self, task_name):
        logging_folder = os.path.join(self._logging_folder, task_name)
        os.makedirs(logging_folder, exist_ok=True)
        logging_path = os.path.join(logging_folder, f"{timestamp()}.log")
        self._setup_logger(task_name, logging_path)

    def _refresh(self, skip_check_finished):
        if self._pid2task_id is None:
            self._pid2task_id = self._resource_manager.pid2task_id
        if not self._resource_manager.inference_usages_initialized:
            self._resource_manager.initialize_inference_usages()
        if not self._resource_manager.checkpoint_initialized:
            return
        self._resource_manager.log_pid_usages_and_inference_frees()
        self._resource_manager.check()
        if not skip_check_finished:
            self._wait_and_handle_finish(wait_until_finish=False)

    def _wait_and_handle_finish(self, wait_until_finish):
        finished_slots = self.__wait(wait_until_finish)
        if not finished_slots:
            return
        if self.terminated:
            self._user_terminate()
        finished_bundle = [[], []]
        for finished_slot in finished_slots[::-1]:
            tuple(map(list.append, finished_bundle, map(
                list.pop, [self._working_task_ids, self._working_processes], [finished_slot] * 2)))
        for task_id, process in zip(*finished_bundle):
            task_name = self._resource_manager.handle_finish(process, task_id)
            if task_name is None:
                continue
            self.del_logger(task_name)

    def _add_new_processes(self):
        n_working = len(self._working_processes)
        n_new_jobs = self._n_jobs - n_working
        n_res = len(self._all_task_ids) - self._cursor
        if n_res > 0:
            n_new_jobs = min(n_new_jobs, n_res)
            for _ in range(n_new_jobs):
                new_task_id = self._all_task_ids[self._cursor]
                self._working_processes.append(self._get_process(new_task_id))
                self._working_task_ids.append(new_task_id)
                self._cursor += 1
            return True
        return n_working > 0

    def _user_terminate(self):
        self._log_meta_msg("`_user_terminate` method hit, joining processes", logging.ERROR)
        for process in self._working_processes:
            if process is None:
                continue
            process.join()
        self._log_meta_msg("processes joined, raising self._ParallelError", logging.ERROR)
        recorded_exceptions = self.exceptions
        if not recorded_exceptions:
            raise self._ParallelError("Parallel terminated by user action")
        else:
            raise self._ParallelError("Parallel terminated by unexpected errors")

    def _set_terminate(self, **kwargs):
        meta = self.meta
        meta["terminated"] = True
        self._rs["__meta__"] = meta
        if not kwargs:
            suffix = ""
        else:
            suffix = f" ({' ; '.join(f'{k}: {v}' for k, v in kwargs.items())})"
        self._log_meta_msg(f"`_set_terminate` method hit{suffix}", logging.ERROR)

    def _get_task_name(self, task_id):
        if task_id is None:
            return
        if self._task_names is None:
            task_name = f"task_{task_id}{self.name_suffix}"
        else:
            task_name = f"{self._task_names[task_id]}{self.name_suffix}"
        self._init_logger(task_name)
        return task_name

    def _f_wrapper(self, task_id, cuda=None):
        task_name = self._get_task_name(task_id)
        logger = self._loggers_[task_name]

        def log_method(msg, msg_level=logging.INFO, frame=None):
            if frame is None:
                frame = inspect.currentframe().f_back
            self.log_msg(logger, msg, msg_level, frame)
            return logger

        def _inner(*args):
            if self.terminated:
                return
            try:
                log_method("task started")
                kwargs = {}
                f_wants_cuda = f_wants_log_method = False
                f_signature = inspect.signature(self._func)
                for name, param in f_signature.parameters.items():
                    if param.kind is inspect.Parameter.VAR_KEYWORD:
                        f_wants_cuda = f_wants_log_method = True
                        break
                    if param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
                        if name == "cuda":
                            f_wants_cuda = True
                            continue
                        if name == "log_method":
                            f_wants_log_method = True
                            continue
                if not f_wants_cuda:
                    if self._use_cuda:
                        log_method("task function doesn't want cuda but cuda is used", logging.WARNING)
                else:
                    log_method("task function wants cuda")
                    kwargs["cuda"] = cuda
                if not f_wants_log_method:
                    log_method("task function doesn't want log_method", logging.WARNING)
                else:
                    log_method("task function wants log_method")
                    kwargs["log_method"] = log_method
                self._rs[task_name] = rs = self._func(*args, **kwargs)
                terminate = isinstance(rs, dict) and rs.get("terminate", False)
                if not terminate:
                    log_method("task finished")
            except KeyboardInterrupt:
                log_method("key board interrupted", logging.ERROR)
                return
            except Exception as err:
                logger.exception(
                    f"exception occurred, {err}",
                    extra={"func_prefix": LoggingMixin._get_func_prefix(None)}
                )
                terminate = True
                exceptions = self.exceptions
                self._rs[task_name] = rs = err
                exceptions[task_name] = rs
                self._rs["__exceptions__"] = exceptions
            if terminate:
                self._set_terminate(scope="f_wrapper", task=task_name)
                log_method("task terminated", logging.ERROR)

        return _inner

    def _get_process(self, task_id, start=True):
        rs = self._resource_manager.get_process(
            task_id, lambda: self.__sleep(skip_check_finished=False), start)
        task_name = rs["__task_name__"]
        if not rs["__create_process__"]:
            return
        if not self._use_cuda or "GPU" not in rs:
            args = (task_id,)
        else:
            args = (task_id, rs["GPU"]["tgt_resource_id"])
        target = self._f_wrapper(*args)
        process = Process(target=target, args=tuple(args[task_id] for args in self._args_list))
        self._log_with_meta(task_name, "process created")
        if start:
            process.start()
            self._log_with_meta(task_name, "process started")
            self._record_process(process, task_id, rs)
            return process
        return process, rs

    def _record_process(self, process, task_id, rs):
        if process is None:
            return
        self._resource_manager.record_process(process, task_id, rs)


__all__ = ["Parallel"]