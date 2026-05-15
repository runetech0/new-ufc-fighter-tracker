class BaseException(Exception):
    pass


class LoginFailedError(BaseException):
    pass


class BrowserError(BaseException):
    pass
