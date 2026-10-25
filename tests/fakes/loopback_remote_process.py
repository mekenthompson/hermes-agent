"""A remote SDK exec boundary backed by real shells in the test namespace."""

import os
import subprocess
import tempfile


class LoopbackRemoteEnvironment:
    """Run SDK requests without enrolling the simulated server in local custody.

    The real native remote producer owns authorization and background receipts.
    Like a remote exec API, each request waits for its shell, while background
    descendants continue independently and write the producer's real artifacts.
    """

    def __init__(self, environment):
        self.cwd = environment.cwd
        self.env = dict(environment.env)

    def execute(self, command, cwd="", *, timeout=120, rewrite_compound_background=True):
        from tools.approval_context import require_live_task_setup

        require_live_task_setup()
        # A background wrapper inherits stdout. A file snapshot captures the
        # completed API request without waiting for that descendant's pipe EOF.
        with tempfile.TemporaryFile() as output:
            with subprocess.Popen(["bash", "-c", command], cwd=cwd or self.cwd,
                    env=os.environ | self.env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT) as request:
                try:
                    code = request.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    request.kill()
                    request.wait()
                    raise
                size = os.fstat(output.fileno()).st_size
                captured = os.pread(output.fileno(), size, 0)
        return {"output": captured.decode("utf-8", errors="replace"), "returncode": code}
