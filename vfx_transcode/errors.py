class TranscodeError(Exception):
    """Fatal, user-facing error. The message is printed without a traceback."""


class ToolError(TranscodeError):
    """An external tool (oiiotool, ffmpeg) failed."""

    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output

    def __str__(self):
        text = super().__str__()
        if self.output:
            text += "\n" + self.output
        return text
