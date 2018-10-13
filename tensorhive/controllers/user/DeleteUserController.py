from sqlalchemy.orm.exc import NoResultFound
from tensorhive.models.User import User
from tensorhive.database import db, flask_app
from flask_jwt_extended import get_jwt_identity
from tensorhive.config import API
import logging
log = logging.getLogger(__name__)
R = API.RESPONSES['user']
G = API.RESPONSES['general']


class DeleteUserController():

    @staticmethod
    def delete(id):
        try:
            # Check identity
            current_user_id = get_jwt_identity()
            assert current_user_id, G['no_identity']

            with flask_app.app_context():
                # User is not allowed to delete his own account
                assert id != current_user_id, R['delete']['self']
                current_user = User.get(current_user_id)
                db.session.add(current_user)

                # Admin priviliges are required
                assert current_user.has_role('admin'), G['unpriviliged']

                # Destroy
                user_to_destroy = User.get(id)
                db.session.add(current_user)
                user_to_destroy.destroy()
        except AssertionError as error_message:
            content, status = {'msg': str(error_message)}, 403
        except NoResultFound:
            content, status = {'msg': R['not_found']}, 404
        except Exception as e:
            content, status = {'msg': G['internal_error'] + str(e)}, 500
        else:
            content, status = {'msg': R['delete']['success']}, 200
        finally:
            return content, status
