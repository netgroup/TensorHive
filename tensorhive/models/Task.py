from sqlalchemy import Column, Integer, String, ForeignKey, Enum, DateTime
from tensorhive.database import Base
from sqlalchemy.orm import relationship, backref
from tensorhive.models.CRUDModel import CRUDModel
from tensorhive.utils.DateUtils import DateUtils
import enum
import logging
log = logging.getLogger(__name__)


class TaskStatus(enum.Enum):
    not_running = 1
    running = 2
    terminated = 3
    unsynchronized = 4


class Task(CRUDModel, Base):  # type: ignore
    __tablename__ = 'tasks'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'))
    user = relationship(
        'User', backref=backref('tasks', passive_deletes=True, cascade='all, delete, delete-orphan'), lazy='subquery')
    host = Column(String(40), nullable=False)
    pid = Column(Integer, nullable=True)
    status = Column(Enum(TaskStatus), default=TaskStatus.not_running, nullable=False)
    command = Column(String(400), nullable=False)
    spawn_at = Column(DateTime, nullable=True)
    terminate_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return '<Task id={id}, user={user}, name={host}, command={command}\n' \
            '\tpid={pid}, status={status}, spawn_at={spawn_at}, terminate_at={terminate_at}>'.format(
                id=self.id,
                user=self.user,
                host=self.host,
                command=self.command,
                pid=self.pid,
                status=self.status.name,
                spawn_at=self.spawn_at,
                terminate_at=self.terminate_at)

    def check_assertions(self):
        pass

    @property
    def as_dict(self):
        return {
            'id': self.id,
            'userId': self.user_id,
            'hostname': self.host,
            'pid': self.pid,
            'status': self.status.name,
            'command': self.command,
            'spawnAt': DateUtils.try_stringify_datetime(self.spawn_at),
            'terminateAt': DateUtils.try_stringify_datetime(self.terminate_at)
        }
