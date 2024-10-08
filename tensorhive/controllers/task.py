from tensorhive.models.Task import Task, TaskStatus
from tensorhive.models.CommandSegment import CommandSegment, CommandSegment2Task, SegmentType
from tensorhive.models.User import User
from tensorhive.models.Job import Job
from tensorhive.core import task_nursery
from tensorhive.core.task_nursery import SpawnError, ExitCodeError
from pssh.exceptions import ConnectionErrorException, AuthenticationException, UnknownHostException
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt_claims
from sqlalchemy.orm.exc import NoResultFound
from tensorhive.config import API
from functools import wraps
from typing import Optional, Callable, Any, Dict, Tuple, List
from datetime import datetime, timedelta
from stringcase import snakecase
import logging
from tensorhive.exceptions.ForbiddenException import ForbiddenException

log = logging.getLogger(__name__)
TASK = API.RESPONSES['task']
SSH = API.RESPONSES['ssh']
SCREEN_SESSIONS = API.RESPONSES['screen-sessions']
GENERAL = API.RESPONSES['general']
"""
This module contains two kinds of controllers:
- production-ready with authorization and authentication
- unprotected core business logic that can be used anywhere

My goal was to separate authorization from controllers' logic, so that
manual and automatic testing doesn't require patching Flask context
(@jwt_required breaks a lot of things)

In before: some controller MUST have camelCased arguments in order to keep up with API.
They are aliased to snake_case immediately inside controller body.
Connexion has this feature under the hood but it does not always work as it should (only in simple cases)
"""

# Typing aliases
Content = Dict[str, Any]
HttpStatusCode = int
TaskId = int
JobId = int


def synchronize(task_id: TaskId) -> None:
    """Updates the state of a Task object stored in database.

    It compares current db record with list of active screen session (their pids in general)
    on node defined by that record (task.user.username@task.host).

    If task_nursery is unable to fetch active screen sessions then
    the new state is always set to unsynchronized.

    If task.pid is not alive (db record is outdated), then it
    makes transition from last known state to a new state:

    state before sync   => state applied after sync
    -----------------------------------------------
    running             => terminated
    unsynchronized      => not_running

    On every state transition job status is synchronized too
    """
    log.debug('Syncing Task {}...'.format(task_id))
    try:
        task = Task.get(task_id)
        parent_job = Job.get(task.job_id)
        assert task.hostname, 'hostname is empty'
        assert parent_job.user, 'user does not exist'
        active_sessions_pids = task_nursery.running(host=task.hostname, user=parent_job.user.username)
    except NoResultFound:
        # This exception must be handled within try/except block when using Task.get()
        # In other words, methods decorated with @synchronize_task_record must handle this case by themselves!
        log.warning(
            'Task {} could not be found (also synchronized). Failing without taking any action...'.format(task_id))
        pass
    except (AssertionError, Exception) as e:
        # task_nursery.running pssh exceptions are also catched here
        log.error('Unable to synchronize Task {}, reason: {}'.format(task_id, e))
        log.debug('Task {} status was: {}'.format(task_id, task.status.name))
        task.status = TaskStatus.unsynchronized
        task.save()
        log.debug('Task {} is now: {}'.format(task_id, task.status.name))
    else:
        log.debug('[BEFORE SYNC] Task {} status was: {}'.format(task_id, task.status.name))
        change_status_msg = '[AFTER SYNC] Task {id} is now: {curr_status}'
        if task.pid not in active_sessions_pids:
            if task.status is TaskStatus.running:
                task.status = TaskStatus.terminated
                log.debug(change_status_msg.format(id=task_id, curr_status=task.status.name))
            if task.status is TaskStatus.unsynchronized:
                task.status = TaskStatus.not_running
                log.debug(change_status_msg.format(id=task_id, curr_status=task.status.name))
            task.pid = None
            task.save()


def synchronize_task_record(func: Callable) -> Callable:
    """Decorated function MUST CONTAIN task id (int), function can take more arguments though.

    In case when task.id could not be obtained from wrapped function's arguments,
    synchronization will be aborted, but it won't affect wrapped function (silent fail).
    """

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            task_id = args[0]
        except IndexError:
            task_id = kwargs.get('id') or kwargs.get('task_id') or kwargs.get('taskId')

        if task_id:
            synchronize(task_id)
        else:
            log.critical('Synchronization aborted!')
            log.critical('Task id not found in {}(), args: {}, kwargs: {}'.format(func.__name__, args, kwargs))
        return func(*args, **kwargs)

    return sync_wrapper


# Controllers
# POST /jobs/{job_id}/tasks
@jwt_required
def create(task: Dict[str, Any], job_id: JobId) -> Tuple[Content, HttpStatusCode]:
    try:
        # User is not allowed to create task for someone else
        job = Job.query.filter(Job.id == job_id).one()
        if not is_admin() and not job.user_id == get_jwt_identity():
            raise ForbiddenException('unauthorized')
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ForbiddenException:
        content, status = {'msg': GENERAL['unprivileged']}, 403
    else:
        content, status = business_create(task, job_id)
    finally:
        return content, status


# GET /tasks/{id}
@jwt_required
def get(id: TaskId) -> Tuple[Content, HttpStatusCode]:
    try:
        task = Task.get(id)
        parent_job = Job.get(task.job_id)
        if not is_admin() and not get_jwt_identity() == parent_job.user_id:
            raise ForbiddenException('not an owner')
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ForbiddenException:
        content, status = {'msg': GENERAL['unprivileged']}, 403
    else:
        content, status = business_get(id)
    finally:
        return content, status


#  GET /tasks?jobId=1?syncAll=1
@jwt_required
def get_all(jobId: Optional[JobId], syncAll: Optional[bool]) -> Tuple[Content, HttpStatusCode]:
    sync_all = syncAll
    job_id = jobId
    try:
        if job_id is not None:
            job = Job.query.filter(Job.id == job_id).one()
            if not is_admin() and not get_jwt_identity() == job.user_id:
                raise ForbiddenException
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ForbiddenException:
        content, status = {'msg': GENERAL['unprivileged']}, 403
    else:
        content, status = business_get_all(job_id, sync_all)
    finally:
        return content, status


# PUT /tasks/{id}
@jwt_required
def update(id: TaskId, newValues: Dict[str, Any]) -> Tuple[Content, HttpStatusCode]:
    try:
        task = Task.get(id)
        parent_job = Job.get(task.job_id)
        if not is_admin() and not parent_job.user_id == get_jwt_identity():
            raise ForbiddenException('not an owner')
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ForbiddenException:
        content, status = {'msg': GENERAL['unprivileged']}, 403
    else:
        content, status = business_update(id, newValues)
    finally:
        return content, status


# DELETE /tasks/{id}
@jwt_required
def destroy(id: TaskId) -> Tuple[Content, HttpStatusCode]:
    try:
        task = Task.get(id)
        parent_job = Job.get(task.job_id)
        if not is_admin() and not parent_job.user_id == get_jwt_identity():
            raise ForbiddenException('not an owner')
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ForbiddenException:
        content, status = {'msg': GENERAL['unprivileged']}, 403
    else:
        content, status = business_destroy(id)
    finally:
        return content, status


# GET /tasks/{id}/log
@jwt_required
def get_log(id: TaskId, tail: bool) -> Tuple[Content, HttpStatusCode]:
    try:
        task = Task.get(id)
        parent_job = Job.get(task.job_id)
        if not is_admin() and not parent_job.user_id == get_jwt_identity():
            raise ForbiddenException("not an owner")
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ForbiddenException:
        content, status = {'msg': GENERAL['unprivileged']}, 403
    else:
        content, status = business_get_log(id, tail)
    finally:
        return content, status


# Business logic


def business_get_all(job_id: Optional[JobId], sync_all: Optional[bool]) -> Tuple[Content, HttpStatusCode]:
    """Fetches all Task records or those within specific job.
    Allows for synchronizing state of each Task out-of-the-box.

    In typical scenario API client would want to get all records without sync and
    then run sync each records individually.
    """
    tasks = []
    if job_id is not None:
        tasks = Task.query.filter(Task.job_id == job_id).all()
    else:
        user_id = get_jwt_identity()
        if user_id is not None:
            jobs = Job.query.filter(Job.user_id == user_id).all()
            for job in jobs:
                job_tasks = Task.query.filter(Task.job_id == job.id).all()
                tasks.extend(job_tasks)

    # Wanted to decouple syncing from dict conversion with 2 oneliners (using list comprehension),
    # but this code is O(n) instead of O(2n)
    results = []
    for task in tasks:
        if sync_all:
            synchronize(task.id)
        results.append(task.as_dict())
    return {'msg': TASK['all']['success'], 'tasks': results}, 200


def business_create(task: Dict[str, Any], job_id: JobId) -> Tuple[Content, HttpStatusCode]:
    """ Creates new Task db record under the given parent job
    and new db records for given command segments of that task.
    """
    try:
        new_task = Task(
            hostname=task['hostname'],
            command=task['command'])
        new_task.gpu_id = parse_gpu_id_from_command(task['command'])
        parent_job = Job.query.filter(Job.id == job_id).one()
        for segment in task['cmdsegments']['params']:
            new_segment = CommandSegment.query.filter(CommandSegment.segment_type == SegmentType.parameter,
                                                      CommandSegment.name == segment['name']).first()
            if (new_segment is None):
                new_segment = CommandSegment(
                    name=segment['name'],
                    _segment_type=SegmentType.parameter)
            new_task.add_cmd_segment(new_segment, segment['value'])
        for segment in task['cmdsegments']['envs']:
            new_segment = CommandSegment.query.filter(CommandSegment.segment_type == SegmentType.env_variable,
                                                      CommandSegment.name == segment['name']).first()
            if (new_segment is None):
                new_segment = CommandSegment(
                    name=segment['name'],
                    _segment_type=SegmentType.env_variable)
            new_task.add_cmd_segment(new_segment, segment['value'])

        new_task.save()
        parent_job.add_task(new_task)
    except KeyError:
        # At least one of required fields was not present
        content, status = {'msg': GENERAL['bad_request']}, 422
    except AssertionError as e:
        content, status = {'msg': TASK['create']['failure']['invalid'].format(reason=e)}, 422
    except Exception as e:
        log.critical(e)
        content, status = {'msg': GENERAL['internal_error']}, 500
    else:
        content, status = {'msg': TASK['create']['success'], 'task': new_task.as_dict()}, 201
    finally:
        return content, status


@synchronize_task_record
def business_get(id: TaskId) -> Tuple[Content, HttpStatusCode]:
    """Fetches one Task db record"""
    try:
        task = Task.get(id)
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except Exception as e:
        log.critical(e)
        content, status = {'msg': GENERAL['internal_error']}, 500
    else:
        content, status = {'msg': TASK['get']['success'], 'task': task.as_dict()}, 200
    finally:
        return content, status


def parse_gpu_id_from_command(value):
    if value.startswith('CUDA_VISIBLE_DEVICES='):
        gpu_id = value.split('CUDA_VISIBLE_DEVICES=')[1][0]
        if gpu_id == ' ':
            return None
        else:
            return int(gpu_id)


def business_update(id: TaskId, newValues: Dict[str, Any]) -> Tuple[Content, HttpStatusCode]:
    """Updates certain fields of a Task db record, including command field and segments."""
    try:
        new_values = newValues
        task = Task.get(id)
        assert task.status is not TaskStatus.running, "Cannot update task which is already running"
        for key, value in new_values.items():
            if key == 'hostname':
                setattr(task, key, value)
            elif key == 'command':
                task.gpu_id = parse_gpu_id_from_command(value)
                setattr(task, key, value)
            elif key == 'cmdsegments':
                # FIXME Somehow the loop doesn't get all of the elements by the first time
                # but repeating it cleares it
                for segment in task.cmd_segments:
                    task.remove_cmd_segment(segment)
                for segment in task.cmd_segments:
                    task.remove_cmd_segment(segment)
                for segment in new_values['cmdsegments']['envs']:
                    new_segment = CommandSegment.query.filter(CommandSegment.segment_type == SegmentType.env_variable,
                                                              CommandSegment.name == segment['name']).first()
                    if (new_segment is None):
                        new_segment = CommandSegment(
                            name=segment['name'],
                            _segment_type=SegmentType.env_variable)
                    task.add_cmd_segment(new_segment, segment['value'])
                for segment in new_values['cmdsegments']['params']:
                    new_segment = CommandSegment.query.filter(CommandSegment.segment_type == SegmentType.parameter,
                                                              CommandSegment.name == segment['name']).first()
                    if (new_segment is None):
                        new_segment = CommandSegment(
                            name=segment['name'],
                            _segment_type=SegmentType.parameter)
                    task.add_cmd_segment(new_segment, segment['value'])
        task.save()
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except AssertionError as e:
        content, status = {'msg': TASK['update']['failure']['assertions'].format(reason=e)}, 422
    except Exception as e:
        log.critical(e)
        content, status = {'msg': GENERAL['internal_error']}, 500
    else:
        content, status = {'msg': TASK['update']['success'], 'task': task.as_dict()}, 201
    finally:
        return content, status


@synchronize_task_record
def business_destroy(id: TaskId) -> Tuple[Content, HttpStatusCode]:
    """Deletes a Task db record. Requires terminating task manually in advance.
    All of the m-n relationship links (task-cmd_segment) are deleted too
    Have to delete unwanted command segments (no task attached) manually
    """
    try:
        task = Task.get(id)
        cmd_segments = task.cmd_segments
        assert task.status is not TaskStatus.running, 'must be terminated first'
        task.destroy()
        for segment in cmd_segments:
            if len(segment.tasks) == 0:
                segment.destroy()
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except AssertionError as e:
        content, status = {'msg': TASK['delete']['failure']['assertions'].format(reason=e)}, 422
    except Exception:
        content, status = {'msg': GENERAL['internal_error']}, 500
    else:
        content, status = {'msg': TASK['delete']['success']}, 200
    finally:
        return content, status


@synchronize_task_record
def business_spawn(id: TaskId) -> Tuple[Content, HttpStatusCode]:
    """Spawns command stored in Task db record (task.full_command).
    It won't allow for spawning task which is currently running (sync + status check).
    If spawn operation has succeeded then `running` status is set.
    """
    try:
        task = Task.get(id)
        parent_job = Job.get(task.job_id)
        assert task.status is not TaskStatus.running, 'task is already running'
        assert task.full_command, 'command is empty'
        assert task.hostname, 'hostname is empty'
        assert parent_job.user, 'user does not exist'

        pid = task_nursery.spawn(task.full_command,
                                 task.hostname,
                                 parent_job.user.username,
                                 name_appendix=str(task.id))
        task.pid = pid
        task.status = TaskStatus.running
        task.save()
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except AssertionError as e:
        content, status = {'msg': TASK['spawn']['failure']['assertions'].format(reason=e)}, 422
    except SpawnError as e:
        log.warning(e)
        content, status = {'msg': TASK['spawn']['failure']['backend'].format(reason=e)}, 500
    except Exception as e:
        log.critical(e)
        content, status = {'msg': GENERAL['internal_error']}, 500
    else:
        log.info('Task {} is now: {}'.format(task.id, task.status.name))
        content, status = {'msg': TASK['spawn']['success'], 'pid': pid}, 200
    finally:
        return content, status


@synchronize_task_record
def business_terminate(id: TaskId, gracefully: Optional[bool] = True) -> Tuple[Content, HttpStatusCode]:
    """Sends SIGINT (default) or SIGKILL to process with pid that is stored in Task db record.

    In order to send SIGKILL, pass `gracefully=False` to `terminate` function.
    Note that:
    1) `exit_code` is related to executing kill operation, not killed process.
    2) termination signal should be respected by most processes, however this
        function does not guarantee stoping the process!
    """
    try:
        task = Task.get(id)
        assert task.status is TaskStatus.running, 'only running tasks can be terminated'
        assert task.pid, 'task has no pid assigned'  # It means there's inconsistency
        parent_job = Job.get(task.job_id)

        # gracefully:
        # True -> interrupt (allows output to be flushed into log file)
        # None -> terminate (works almost every time, but losing output that could be produced before closing)
        # False -> kill (similar to above, but success is almost guaranteed)
        exit_code = task_nursery.terminate(task.pid, task.hostname, parent_job.user.username, gracefully=gracefully)
        if exit_code != 0:
            raise ExitCodeError('operation exit code is not 0')

        # Note: Code below is unsafe, because interrupt and terminate does not guarantee success.
        # It's better to let synchhronization update this (via comparison with screen sessions)
        # (Unsafe section) Original comment: Allow to spawn that task again
        # task.pid = None
        # task.status = TaskStatus.terminated

        task.save()
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ExitCodeError:
        content, status = {'msg': TASK['terminate']['failure']['exit_code'], 'exit_code': exit_code}, 202
    except AssertionError as e:
        content, status = {'msg': TASK['terminate']['failure']['state'].format(reason=e)}, 409
    except ConnectionErrorException as e:
        content, status = {'msg': TASK['failure']['connection'].format(reason=e)}, 500
    except Exception as e:
        log.critical(e)
        content, status = {'msg': GENERAL['internal_error']}, 500
    else:
        content, status = {'msg': TASK['terminate']['success'], 'exit_code': exit_code}, 200
    finally:
        return content, status


def business_get_log(id: TaskId, tail: bool) -> Tuple[Content, HttpStatusCode]:
    """Fetches log file created by spawned task (output redirection).

    It relies on reading files located on filesystem, via connection with `parent_job.user.username@task.host`
    If file does not exist there's no way to fetch it from database (currently).
    File names must be named in one fashion (standard defined in `task_nursery.fetch_log`,
    currently: `task_<id>.log`). Renaming them manually will lead to inconsistency or 'Not Found' errors.

    `tail` argument allows for returning only the last few lines (10 is default for `tail` program).
    For more details, see,: `task_nursery.fetch_log`.
    """
    try:
        task = Task.get(id)
        parent_job = Job.get(task.job_id)
        assert task.hostname, 'hostname is empty'
        assert parent_job.user, 'user does not exist'
        output_gen, log_path = task_nursery.fetch_log(task.hostname, parent_job.user.username, task.id, tail)
    except NoResultFound:
        content, status = {'msg': TASK['not_found']}, 404
    except ExitCodeError as e:
        content, status = {'msg': TASK['get_log']['failure']['not_found'].format(location=e)}, 404
    except AssertionError as e:
        content, status = {'msg': TASK['get_log']['failure']['assertions'].format(reason=e)}, 422
    except (ConnectionErrorException, AuthenticationException, UnknownHostException) as e:
        content, status = {'msg': SSH['failure']['connection'].format(reason=e)}, 500
    except Exception as e:
        log.critical(e)
        content, status = {'msg': GENERAL['internal_error']}, 500
    else:
        content, status = {'msg': TASK['get_log']['success'], 'path': log_path, 'output_lines': list(output_gen)}, 200
    finally:
        return content, status


def is_admin() -> bool:
    return 'admin' in get_jwt_claims()['roles']
