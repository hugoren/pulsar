'''
The :class:`TaskBackend` is at the hart of the
:ref:`task queue application <apps-taskqueue>`. It exposes
all the functionalities for running new tasks, scheduling periodic tasks
and retrieving task information. Pulsar ships with two backends, one which uses
pulsar internals and store tasks in the arbiter domain and another which stores
tasks in redis_.


Implementing a Task Backend
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
When creating a new :class:`TaskBackend` there are five methods which must
be implemented:

* The :meth:`TaskBackend.put_task` method, invoked when putting a new
  :class:`Task.id` into the distributed task queue, whatever that is.
* The :meth:`TaskBackend.get_task` method, invoked when retrieving
  a :class:`Task` from the backend server.
* The :meth:`TaskBackend.get_tasks` method, invoked when retrieving
  a group of :class:`Task` from the backend server.
* The :meth:`TaskBackend.save_task` method, invoked when creating
  or updating a :class:`Task`.
* The :meth:`TaskBackend.delete_tasks` method, invoked when deleting
  a bunch of :class:`Task`.


Task
~~~~~~~~~~~~~

.. autoclass:: Task
   :members:
   :member-order: bysource
      
   
TaskBackend
~~~~~~~~~~~~~

.. autoclass:: TaskBackend
   :members:
   :member-order: bysource


.. _task-state:

Task states
~~~~~~~~~~~~~

A :class:`Task` can have one of the following :attr:`Task.status` string:

* ``PENDING`` A task waiting to be queued for execution.
* ``QUEUED`` A task queued but not yet executed.
* ``RETRY`` A task is retrying calculation.
* ``STARTED`` task where execution has started.
* ``REVOKED`` the task execution has been revoked. One possible reason could be
  the task has timed out.
* ``UNKNOWN`` task execution is unknown.
* ``FAILURE`` task execution has finished with failure.
* ``SUCCESS`` task execution has finished with success.


**FULL_RUN_STATES**

The set of states for which a :class:`Task` has run:
``FAILURE`` and ``SUCCESS``


**READY_STATES**

The set of states for which a :class:`Task` has finished:
``REVOKED``, ``FAILURE`` and ``SUCCESS``
   
   
Scheduler Entry
~~~~~~~~~~~~~~~~~~~

.. autoclass:: SchedulerEntry
   :members:
   :member-order: bysource
   
.. _redis: http://redis.io/
'''
import sys
import logging
from collections import deque
from datetime import datetime, timedelta
from functools import partial

from pulsar import async, EMPTY_TUPLE, EMPTY_DICT, get_actor, log_failure,\
                maybe_failure, is_failure, PulsarException, Backend, Deferred
from pulsar.utils.pep import itervalues, iteritems
from pulsar.apps.tasks.models import JobRegistry
from pulsar.apps.tasks import states, create_task_id
from pulsar.utils.timeutils import remaining, timedelta_seconds
from pulsar.utils.log import local_property

__all__ = ['Task', 'Backend', 'TaskBackend', 'TaskNotAvailable',
           'nice_task_message']


LOGGER = logging.getLogger('pulsar.tasks')


def get_datetime(expiry, start):
    if isinstance(expiry, datetime):
        return expiry
    elif isinstance(expiry, timedelta):
        return start + expiry
    else:
        return datetime.fromtimestamp(expiry)

def format_time(dt):
    return dt.isoformat() if dt else '?'

def nice_task_message(req, smart_time=None):
    smart_time = smart_time or format_time
    status = req['status'].lower()
    user = req.get('user')
    ti = req.get('time_start', req.get('time_executed'))
    name = '%s (%s) ' % (req['name'], req['id'][:8])
    msg = '%s %s at %s' % (name, status, smart_time(ti))
    return '%s by %s' % (msg, user) if user else msg

class TaskNotAvailable(PulsarException):
    MESSAGE = 'Task {0} is not registered. Check your settings.'
    def __init__(self, task_name):
        self.task_name = task_name
        super(TaskNotAvailable,self).__init__(self.MESSAGE.format(task_name))
        
        
class TaskTimeout(PulsarException):
    pass


class TaskConsumer(object):
    '''A context manager for consuming tasks.

.. attribute:: task_id

    the :attr:`Task.id` being consumed.

.. attribute:: job

    the :class:`Job` which generated the :attr:`task`.

.. attribute:: worker

    the :class:`pulsar.apps.Worker` running the process.
    
.. attribute:: backend

    give access to the :class:`TaskBackend`.
'''
    def __init__(self, backend, worker, task_id, job):
        self.backend = backend
        self.worker = worker
        self.job = job
        self.task_id = task_id
    
    
class Task(object):
    '''Interface for tasks which are produced by
:ref:`jobs or periodic jobs <apps-taskqueue-job>`.

.. attribute:: id

    :class:`Task` unique id.

.. attribute:: name

    :class:`Job` name.

.. attribute:: status

    The current :ref:`status string <task-state>` of task.

.. attribute:: time_executed

    date time when the task was executed.

.. attribute:: time_start

    date-time when the task calculation has started.

.. attribute:: time_end

    date-time when the task has finished.

.. attribute:: expiry

    optional date-time indicating when the task should expire.

.. attribute:: timeout

    A datetime or ``None`` indicating whether a timeout has occurred.

.. attribute:: from_task

    Optional :attr:`Task.id` for the :class:`Task` which queued
    this :class:`Task`. This is a usuful for monitoring the creation
    of tasks within other tasks.
'''
    stack_trace = None
    def __init__(self, id, name=None, time_executed=None,
                 expiry=None, args=None, kwargs=None, 
                 status=None, from_task=None, result=None, **params):
        self.id = id
        self.name = name
        self.time_executed = time_executed
        self.from_task = from_task
        self.time_started = None
        self.time_ended = None
        self.expiry = expiry
        self.args = args
        self.kwargs = kwargs
        self.status = status
        self.result = result
        self.params = params

    def __repr__(self):
        return '%s (%s)' % (self.name, self.id)
    __str__ = __repr__
    
    @property
    def status_code(self):
        '''Integer indicating :attr:`status` precedence.
Lower number higher precedence.'''
        return states.PRECEDENCE_MAPPING.get(self.status, states.UNKNOWN_STATE)

    def done(self):
        '''Return ``True`` if the :class:`Task` has finshed
(its status is one of :ref:`READY_STATES <task-state>`).'''
        return self.status in states.READY_STATES

    def execute2start(self):
        if self.time_start:
            return self.time_start - self.time_executed

    def execute2end(self):
        if self.time_end:
            return self.time_ended - self.time_executed

    def duration(self):
        '''The :class:`Task` duration. Only available if the task status is in
:attr:`FULL_RUN_STATES`.'''
        if self.time_end and self.time_started:
            return self.time_ended - self.time_started

    def tojson(self):
        '''Convert the task instance into a JSON-serializable dictionary.'''
        return self.__dict__.copy()
    

class TaskCallbacks(object):
    '''Calass for handling tasks callbacks'''
    def __init__(self):
        self.callbacks = {}
        
    def finish(self, task):
        '''Finish a task by revoking it if not yet done and calling
back the callback if not already called back.'''
        if not task.done():
            task.status = states.REVOKED
        return self._pop(task)

    def when_done(self, task):
        if task.done(): # the task is done already
            return self._pop(task)
        else:
            when_done = self.callbacks.get(task.id)
            if not when_done:
                self.callbacks[task.id] = when_done = Deferred()
            return when_done
    
    def _pop(self, task):
        when_done = self.callbacks.pop(task.id, None)
        if when_done:
            return when_done.callback(task)
        else:
            return task
    
    
class TaskBackend(Backend):
    '''A :class:`pulsar.apps.Backend` class for :class:`Task`.
A :class:`TaskBackend` is responsible for creating tasks and put them
into the distributed queue.
It also schedules the run of periodic tasks if enabled to do so.
    
.. attribute:: task_paths

    List of paths where to upload :ref:`jobs <app-taskqueue-job>` which
    are factory of tasks. Passed by the task-queue application
    :ref:`task paths setting <setting-task_paths>`.
    
.. attribute:: schedule_periodic

    `True` if this :class:`TaskBackend` can schedule periodic tasks. Passed
    by the task-queue application
    :ref:`schedule-periodic setting <setting-schedule_periodic>`.
    
.. attribute:: backlog

    The maximum number of concurrent tasks running on a task-queue
    :class:`pulsar.apps.Worker`. A number in the order of 5 to 10 is normally
    used. Passed by the task-queue application
    :ref:`backlog setting <setting-backlog>`.
'''
    default_path = 'pulsar.apps.tasks.backends.%s'
    
    def setup(self, task_paths=None, schedule_periodic=False, backlog=1,
              **params):
        self.task_paths = task_paths
        self.backlog = backlog
        self.local.schedule_periodic = schedule_periodic
        self.next_run = datetime.now()
        return params
        
    @property
    def schedule_periodic(self):
        return self.local.schedule_periodic
    
    @local_property
    def concurrent_requests(self):
        return 0
    
    @local_property
    def entries(self):
        return self._setup_schedule()
    
    @local_property
    def registry(self):
        '''The :class:`pulsar.apps.tasks.models.JobRegistry` for this backend.'''
        return JobRegistry.load(self.task_paths)
    
    def start(self, worker):
        '''Start this :class:`TaskBackend`. Invoked by the
:class:`pulsar.apps.Worker` which is ready to consume tasks.'''
        worker.create_thread_pool()
        self.local.task_poller = worker.event_loop.call_soon(
                                    self.may_pool_task, worker)
        LOGGER.debug('%s started polling tasks', worker)
        
    def close(self, worker):
        '''Close this :class:`TaskBackend`. Invoked by the
:class:`pulsar.apps.Worker` when is stopping.'''
        if self.local.task_poller:
            self.local.task_poller.cancel()
            LOGGER.debug('%s stopped polling tasks', worker)
        
    def run(self, jobname, *args, **kwargs):
        '''A shortcut for :meth:`run_job` without task meta parameters'''
        return self.run_job(jobname, args, kwargs)
    
    def run_job(self, jobname, targs=None, tkwargs=None, **meta_params):
        '''Create a new :ref:`task <apps-taskqueue-task>` which may or
may not be queued. This method returns a :ref:`coroutine <coroutine>`.
If *jobname* is not a valid :attr:`pulsar.apps.tasks.models.Job.name`,
a ``TaskNotAvailable`` exception occurs.

:parameter jobname: the name of a :class:`Job` registered
    with the :class:`TaskQueue` application.
:parameter targs: optional tuple used for the positional arguments in the
    task callable.
:parameter tkwargs: optional dictionary used for the key-valued arguments
    in the task callable.
:parameter meta_params: Additional parameters to be passed to the :class:`Task`
    constructor (not its callable function).
:return: a :ref:`coroutine <coroutine>` resulting in a :attr:`Task.id`
    on success.'''
        return self._run_job(jobname, targs, tkwargs, meta_params)\
                   .add_errback(log_failure)
        
    def create_task_id(self, job, args, kwargs):
        '''Create a :attr:`Task.id` from *job*, positional arguments *args*
and key-valued arguments *kwargs*.'''
        return job.make_task_id(args, kwargs)
        
    def create_task(self, jobname, targs=None, tkwargs=None, expiry=None,
                    **params):
        '''Create a new :class:`Task` from ``jobname``, positional arguments
``targs``, key-valued arguments ``tkwargs`` and :class:`Task` meta parameters
``params``. 
        
:param jobname: the name of job which create the task.
:param targs: task positional arguments (a ``tuple`` or ``None``).
:param tkwargs: task key-valued arguments (a ``dict`` or ``None``).
:return: a :ref:`coroutine <coroutine>` resulting in a :attr:`Task.id`
    or ``None`` if no task was created.
'''
        if jobname in self.registry:
            job = self.registry[jobname]
            targs = targs or EMPTY_TUPLE
            tkwargs = tkwargs or EMPTY_DICT
            task_id = self.create_task_id(job, targs, tkwargs)
            task = yield self.get_task(task_id)
            if task:
                # the task with id is already available
                if task.done():
                    task = yield self.handle_task_done(task)
            if task:
                LOGGER.debug('Task %s already requested, abort.', task)
                yield None
            else:
                if self.entries and job.name in self.entries:
                    self.entries[job.name].next()
                time_executed = datetime.now()
                if expiry is not None:
                    expiry = get_datetime(expiry, time_executed)
                elif job.timeout:
                    expiry = get_datetime(job.timeout, time_executed)
                LOGGER.debug('Queue new task %s (%s).', job.name, task_id)
                yield self.save_task(task_id, name=job.name,
                                     time_executed=time_executed,
                                     expiry=expiry, args=targs, kwargs=tkwargs,
                                     status=states.PENDING, **params)
        else:
            raise TaskNotAvailable(jobname)
    
    def job_list(self, jobnames=None):
        registry = self.registry
        jobnames = jobnames or registry
        all = []
        for name in jobnames:
            if name not in registry:
                continue
            job = registry[name]
            can_overlap = job.can_overlap
            if hasattr(can_overlap, '__call__'):
                can_overlap = 'maybe'
            d = {'doc':job.__doc__,
                 'doc_syntax':job.doc_syntax,
                 'type':job.type,
                 'can_overlap': can_overlap}
            if self.entries and name in self.entries:
                entry = self.entries[name]
                _,next_time_to_run = self.next_scheduled((name,))
                run_every = 86400*job.run_every.days + job.run_every.seconds
                d.update({'next_run':next_time_to_run,
                          'run_every':run_every,
                          'runs_count':entry.total_run_count})
            all.append((name,d))
        return all
            
    def next_scheduled(self, jobnames=None):
        if not self.schedule_periodic:
            return
        if jobnames:
            entries = (self.entries.get(name, None) for name in jobnames)
        else:
            entries = itervalues(self.entries)
        next_entry = None
        next_time = None
        for entry in entries:
            if entry is None:
                continue
            is_due, next_time_to_run = entry.is_due()
            if is_due:
                next_time = 0
                next_entry = entry
                break
            elif next_time_to_run is not None:
                if next_time is None or next_time_to_run < next_time:
                    next_time = next_time_to_run
                    next_entry = entry
        if next_entry:
            return (next_entry.name, max(next_time, 0))
        else:
            return (jobnames, None)
    
    def handle_task_done(self, task):
        new_id = create_task_id()
        data = task.tojson()
        data.pop('id')
        yield self.delete_tasks([task.id])
        yield self.save_task(new_id, **data)
        yield None
        
    def wait_for_task(self, task_id):
        '''Asynchronously wait for a task to have finish its execution. It
returns an `asynchronous component <tutorials-coroutine>`_'''
        return self.get_task(task_id, when_done=True)
            
    def tick(self, now=None):
        '''Run a tick, that is one iteration of the scheduler. This
method only works when :attr:`schedule_periodic` is ``True`` and
the arbiter context.

Executes all due tasks and calculate the time in seconds to wait before
running a new :meth:`tick`. For testing purposes a :class:`datetime.datetime`
value ``now`` can be passed.'''
        if not self.schedule_periodic: 
            return
        remaining_times = []
        try:
            for entry in itervalues(self.entries):
                is_due, next_time_to_run = entry.is_due(now=now)
                if is_due:
                    self.run_job(entry.name)
                if next_time_to_run:
                    remaining_times.append(next_time_to_run)
        except Exception:
            LOGGER.exception('Unhandled error in task backend')
        self.next_run = now or datetime.now()
        if remaining_times:
            self.next_run += timedelta(seconds = min(remaining_times))
     
    ############################################################################
    ##    HOOKS
    ############################################################################
    def on_start_task(self, consumer):
        '''Called once a new task has started its execution in the thread
pool.'''
        pass
    
    def on_finish_task(self, consumer):
        '''Called once a new task has finished its execution in the thread
pool.'''
        pass
    
    ############################################################################
    ##    ABSTRACT METHODS
    ############################################################################
    def put_task(self, task_id):
        '''Put the ``task_id`` into the queue. Must be implemented
by subclasses.'''
        raise NotImplementedError
    
    def num_tasks(self):
        '''Retrieve the number of tasks in the task queue.'''
        raise NotImplementedError
    
    def get_task(self, task_id=None, when_done=False, timeout=1):
        '''Retrieve a :class:`Task` from a ``task_id``. Must be implemented
by subclasses.

:param task_id: the :attr:`Task.id` of the task to retrieve.
:param when_done: if ``True`` return only when the task is in a ready state.
:param timeout: timeout to use when polling a task from the distributed queue.
:return: a :class:`Task` or ``None``.
'''
        raise NotImplementedError
    
    def get_tasks(self, **filters):
        '''Retrieve a group of :class:`Task` from the backend.'''
        raise NotImplementedError
    
    def save_task(self, task_id, **params):
        '''Create or update a :class:`Task` with ``task_id`` and key-valued
parameters ``params``. Must be implemented by subclasses.'''
        raise NotImplementedError
    
    def delete_tasks(self, task_ids=None):
        '''Delete a group of task. Must be implemented by subclasses.'''
        raise NotImplementedError
    
    ############################################################################
    ##    PRIVATE METHODS
    ############################################################################
    @async()
    def may_pool_task(self, worker):
        '''Called at every loop in the worker IO loop, it pool a new task
if possible and add it to the queue of tasks consumed by the worker
CPU-bound thread.'''
        while worker.running:
            thread_pool = worker.thread_pool
            if not thread_pool:
                LOGGER.warning('No thread pool, cannot poll tasks.')
            elif self.concurrent_requests < self.backlog:
                task = yield self.get_task()
                if task:
                    self.local.concurrent_requests += 1
                    thread_pool.apply_async(self._execute_task, (worker, task))
            else:
                LOGGER.info('Cannot poll tasks. %s concurrent requests.',
                            self.concurrent_requests)
                break
        worker.event_loop.call_soon(self.may_pool_task, worker)
            
    @async(max_errors=None)
    def _execute_task(self, worker, task):
        #Asynchronous execution of a :class:`Task`. This method is called
        #on a separate thread of execution from the worker event loop thread.
        task_id = task.id
        result = None
        status = None
        consumer = None
        time_ended = datetime.now()
        try:
            job = self.registry.get(task.name)
            consumer = TaskConsumer(self, worker, task_id, job)
            if not consumer.job:
                raise RuntimeError('Task "%s" not in registry %s' %
                                   (task.name, self.registry))
            if task.status_code > states.PRECEDENCE_MAPPING[states.STARTED]:
                if task.expiry and time_ended > task.expiry:
                    raise TaskTimeout
                else:
                    LOGGER.debug('starting task %s', task)
                    yield self.save_task(task_id, status=states.STARTED,
                                         time_started=time_ended)
                    yield self.on_start_task(consumer)
                    result = yield job(consumer, *task.args, **task.kwargs)
                    time_ended = datetime.now()
            else:
                consumer = None
        except TaskTimeout:
            LOGGER.debug('Task %s timed-out', task)
            status = states.REVOKED
        except:
            result = maybe_failure(sys.exc_info())
        if is_failure(result):
            result.log()
            status = states.FAILURE
            result = str(result)
        elif not status:
            status = states.SUCCESS
        if consumer:
            yield self.save_task(task_id, time_ended=time_ended,
                                 status=status, result=result)
            LOGGER.debug('Finished task %s', task)
            yield self.on_finish_task(consumer)
        worker.event_loop.call_soon_threadsafe(self._done_task, task_id)
        yield task_id
        
    def _done_task(self, task_id):
        self.local.concurrent_requests -= 1
        
    def _setup_schedule(self):
        if not self.local.schedule_periodic:
            return
        entries = {}
        for name, task in self.registry.filter_types('periodic'):
            schedule = self._maybe_schedule(task.run_every, task.anchor)
            entries[name] = SchedulerEntry(name, schedule)
        return entries
    
    def _maybe_schedule(self, s, anchor):
        if not self.local.schedule_periodic:
            return
        if isinstance(s, int):
            s = timedelta(seconds=s)
        if not isinstance(s, timedelta):
            raise ValueError('Schedule %s is not a timedelta' % s)
        return Schedule(s, anchor)
    
    @async(get_result=False)
    def _run_job(self, jobname, targs, tkwargs, meta_params):
        # Create a new task and put it in the task queue
        task_id = yield self.create_task(jobname, targs, tkwargs, **meta_params)
        if task_id:
            yield self.put_task(task_id)
        yield task_id
    
    
class Schedule(object):

    def __init__(self, run_every=None, anchor=None):
        self.run_every = run_every
        self.anchor = anchor

    def remaining_estimate(self, last_run_at, now=None):
        """Returns when the periodic task should run next as a timedelta."""
        return remaining(last_run_at, self.run_every, now=now)

    def is_due(self, last_run_at, now=None):
        """Returns tuple of two items ``(is_due, next_time_to_run)``,
        where next time to run is in seconds.

        See :meth:`unuk.contrib.tasks.models.PeriodicTask.is_due` for more information.
        """
        rem_delta = self.remaining_estimate(last_run_at, now = now)
        rem = timedelta_seconds(rem_delta)
        if rem == 0:
            return True, timedelta_seconds(self.run_every)
        return False, rem


class SchedulerEntry(object):
    """A class used as a schedule entry in by a :class:`Scheduler`."""
    name = None
    '''Task name'''
    schedule = None
    '''The schedule'''
    last_run_at = None
    '''The time and date of when this task was last run.'''
    total_run_count = None
    '''Total number of times this periodic task has been executed by the
    :class:`Scheduler`.'''

    def __init__(self, name, schedule, args=(), kwargs={},
                 last_run_at = None, total_run_count=None):
        self.name = name
        self.schedule = schedule
        self.last_run_at = last_run_at or datetime.now()
        self.total_run_count = total_run_count or 0

    def __repr__(self):
        return self.name
    __str__ = __repr__

    @property
    def scheduled_last_run_at(self):
        '''The scheduled last run datetime. This is different from
:attr:`last_run_at` only when :attr:`anchor` is set.'''
        last_run_at = self.last_run_at
        anchor = self.anchor
        if last_run_at and anchor:
            run_every = self.run_every
            times = int(timedelta_seconds(last_run_at - anchor)\
                            /timedelta_seconds(run_every))
            if times:
                anchor += times*run_every
                while anchor <= last_run_at:
                    anchor += run_every
                while anchor > last_run_at:
                    anchor -= run_every
                self.schedule.anchor = anchor
            return anchor
        else:
            return last_run_at

    @property
    def run_every(self):
        return self.schedule.run_every

    @property
    def anchor(self):
        return self.schedule.anchor

    def next(self, now = None):
        """Returns a new instance of the same class, but with
        its date and count fields updated. Function called by :class:`Scheduler`
        when the ``this`` is due to run."""
        now = now or datetime.now()
        self.last_run_at = now or datetime.now()
        self.total_run_count += 1
        return self

    def is_due(self, now = None):
        return self.schedule.is_due(self.scheduled_last_run_at, now=now)