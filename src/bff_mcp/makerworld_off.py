"""Stand-in for makerworld.py in builds without MakerWorld sign-in.

MakerWorld downloads need a non-public account API, so the public build leaves
makerworld.py out and server.py falls back to this module: same names, no network.
"""

NOT_HERE = "MakerWorld links need sign-in, not in this build: download the model from MakerWorld and use Upload"


class LoginNeeded(Exception):
    pass


class LoginFailed(Exception):
    pass


def fetch(db, url):
    raise LoginFailed(NOT_HERE)


def login_start(db, email):
    raise LoginFailed(NOT_HERE)


def login_code(db, code):
    raise LoginFailed(NOT_HERE)


def status(db):
    return {"available": False, "logged_in": False, "email": None}


def logout(db):
    pass
