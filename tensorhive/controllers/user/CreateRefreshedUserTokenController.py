from flask_jwt_extended import create_access_token, get_jwt_identity
from tensorhive.models.User import User
from sqlalchemy.orm.exc import NoResultFound
from tensorhive.config import API
import logging
log = logging.getLogger(__name__)
R = API.RESPONSES
G = API.RESPONSES['general']


class CreateRefreshedUserTokenController():

    @staticmethod
    def create():
        try:
            current_user = User.get(id=get_jwt_identity())
            new_access_token = create_access_token(identity=current_user.id, fresh=False)
        except NoResultFound as e:
            log.error(e)
            content = {'msg': R['refresh']['failure']}
            status = 401
        except Exception as e:
            log.critical(e)
            content = {'msg': G['internal_error']}
            status = 500
        else:
            content = {
                'msg': R['token']['refresh']['success'],
                'access_token': new_access_token
            }
            status = 200
        finally:
            return content, status
