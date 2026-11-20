class CLIError(Exception):
    """A safe, concise error that can be shown to the user."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code
