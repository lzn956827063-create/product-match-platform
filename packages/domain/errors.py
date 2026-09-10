class Problem(Exception):
    def __init__(self, status, code, message, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details


def require(condition, status, code, message, details=None):
    if not condition:
        raise Problem(status, code, message, details)
