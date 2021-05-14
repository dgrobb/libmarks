from __future__ import print_function
import contextlib
import sys
import os
import inspect
import difflib
import time
import pathlib
import shutil

from .result import TestResult
from .util import strclass, safe_repr, coloured_text
from .procs import xProcess, xTimeoutProcess, xTracedProcess, ExplainProcess


BUFFER_SIZE = 8 * 1024


def marks(category, mark=None, category_marks=None):
    """Assign marks to a test or suite of tests, grouped by a category."""

    def decorator(test_item):
        if mark is None and category_marks is None:
            raise ValueError("One of mark or category_marks must be defined")
        test_item.__marks_category__ = category
        test_item.__marks_mark__ = mark
        test_item.__marks_category_marks__ = category_marks
        return test_item

    return decorator


def ignore_result(test_item):
    """Mark a test as having its results ignored.
    Used for tests that add details but do not test functionality
    """
    test_item.__marks_ignore_result__ = True
    return test_item


class _TestWrapper(object):
    def __init__(self):
        self.success = True
        self.errors = []

    @contextlib.contextmanager
    def test_executer(self, test_case, is_test=False):
        old_success = self.success
        self.success = True
        try:
            yield
        except KeyboardInterrupt:
            raise
        except:
            exc_info = sys.exc_info()
            self.success = False
            self.errors.append((test_case, exc_info))
            exc_info = None
        finally:
            self.success = self.success and old_success


class TestCase(object):

    failure_exception = AssertionError
    """The exception to treat as a test failure"""
    default_test_method = "run_test"
    """Default name for the test method"""
    process_class = xProcess
    """Class for a Process"""
    timeout = None
    """Timeout duration, in seconds"""

    def __init__(self, test_method_name="run_test", timeout=None):
        try:
            getattr(self, test_method_name)
        except AttributeError:
            if test_method_name != self.default_test_method:
                raise ValueError(
                    f"test method {strclass(self.__class__)} "
                    + f"does not exist in {test_method_name}"
                )
        else:
            self._test_method = test_method_name

        if timeout:
            self.timeout = timeout

        # Change the process class to one that supports timeout if
        # timeout is set.
        if self.timeout and self.process_class == xProcess:
            self.process_class = xTimeoutProcess

        # Keep track of processes within a test.
        self._process_count = 0
        self._processes = []

        # Dict to collect information about tests.
        self.__details = {}

    def setup(self):
        pass

    def tear_down(self):
        pass

    @classmethod
    def setup_class(cls):
        pass

    @classmethod
    def tear_down_class(cls):
        pass

    def id(self):
        return f"{strclass(self.__class__)}.{self._test_method}"

    def doc(self):
        """Return the docstring of the current test method"""
        return inspect.getdoc(self.test_method)

    def __str__(self):
        return f"{self._test_method} ({strclass(self.__class__)})"

    def __repr__(self):
        return f"<{strclass(self.__class__)} test_method={self._test_method}>"

    def __call__(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def _process_errors(self, result, errors):
        for test_case, exc_info in errors:
            if exc_info is not None:
                if issubclass(exc_info[0], self.failure_exception):
                    result.add_failure(test_case, exc_info)
                else:
                    result.add_error(test_case, exc_info)

    @property
    def test_method(self):
        return getattr(self, self._test_method)

    def _stdout_filename(self, p):
        """
        Generate filename for standard out (stdout) output from a process.
        """
        return f"{self.id()}.{p.count}.out"

    def _stderr_filename(self, p):
        """
        Generate filename for standard error (stderr) output from a process.
        """
        return f"{self.id()}.{p.count}.err"

    def option(self, option, default=None):
        """Retrieves the value of an option, or default if option not set."""
        return self.__marks_options__.get(option, default)

    def _print_coloured(self, text, fg=None, bg=None, attrs=None, **kwargs):
        stream = kwargs.get("file", sys.stdout)
        if stream.isatty():
            # Only add colours and attributes if stream is a TTY.
            text = coloured_text(text, colour=fg, background=bg, attrs=attrs)
        print(text, **kwargs)

    def process(self, argv, input_file=None, *args, **kwargs):
        """Create a Process of the type specified for this test case"""
        # Add the timeout to the init args.
        if self.timeout:
            kwargs.setdefault("timeout", self.timeout)

        # Ensure the timeout is an integer.
        if kwargs.get("timeout") is not None and not isinstance(kwargs["timeout"], int):
            raise ValueError("Process timeout must be an integer.")

        # Include the input file if it is set.
        if input_file is not None:
            kwargs["input_file"] = input_file

        if self.option("explain"):
            # Ensure a real process is not created in export mode.
            self.process_class = ExplainProcess

        # Instantiate the new process.
        p = self.process_class(argv, *args, **kwargs)

        # Store the process count on the process.
        p.count = self._process_count

        # Increment the process count, ready for the next process.
        self._process_count += 1
        self._processes.append(p)

        if self.option("explain"):
            for i, arg in enumerate(argv):
                # Put quotes around argument if it contains whitespace.
                if arg == "" or any(c.isspace() for c in arg):
                    argv[i] = f'"{arg}"'
                # Ensure escape characters are visible when printed.
                # argv[i] = argv[i].encode('unicode_escape')
                # Todo: not sure if repr() would be better
                # argv[i] = argv[i].encode('unicode_escape').decode()
                argv[i] = repr(argv[i]).lstrip("'").rstrip("'")

            # Print out command for running the process, including streams.
            self._print_coloured(f"Start Process {p.count}:", attrs=["bold"])
            print(f"\t{' '.join(argv)}", end="")
            if input_file is not None:
                print(f" < {input_file}", end="")
            print(f" > {self._stdout_filename(p)} 2> {self._stderr_filename(p)}")

        return p

    def _cleanup_processes(self):
        """Attempt to kill all processes started within a test"""
        if self.option("explain"):
            # Do not cleanup, as no processes are running.
            return

        for p in self._processes:
            try:
                p.kill()
            except RuntimeError:
                # Sending signal to process may have failed.
                # This is most likely due to the process already
                # being dead, so ignore.
                pass

    def run(self, result=None, **kwargs):
        original_result = result
        if result is None:
            result = TestResult()
            result.start_test_run()

        # Check if test has its results being ignored.
        ignored = getattr(self.test_method, "__marks_ignore_result__", False)

        if not ignored:
            result.start_test(self)

        # Reset count for processes within test
        self._process_count = 0
        self._processes = []

        # Reset information collected for this test.
        self.__details = {}

        wrapper = _TestWrapper()
        try:
            # Perform setup.
            with wrapper.test_executer(self):
                self.setup()

            if wrapper.success:
                # Run the test method.
                with wrapper.test_executer(self, is_test=True):
                    self.test_method()

                # Perform tear down.
                with wrapper.test_executer(self):
                    self.tear_down()

                # Clean up processes.
                self._cleanup_processes()

            # Process details.
            self._process_details(result)

            # Process wrapper.
            if not ignored:
                self._process_errors(result, wrapper.errors)
                if wrapper.success:
                    result.add_success(self)

            return result
        except KeyboardInterrupt:
            # Clean up processes before raising exception.
            self._cleanup_processes()
            raise
        finally:
            if not ignored:
                result.stop_test(self)
            if original_result is None:
                # One-off test, so finish tests.
                result.stop_test_run()

    def _check_signal(self, process, msg):
        """Check if process was signalled, causing the current test to fail."""
        if process.check_signalled():
            msg += f"\n{' ' * 8}Process received unexpected signal: {process.signal}"
        self._check_timeout(process, msg)

    def _check_timeout(self, process, msg):
        """Check if process was timed out, causing the test to fail."""
        if self.option("update"):
            # Ignore errors when in update mode.
            return
        if process.timeout():
            msg = "Timeout occurred"

        # Kill process, to ensure it is not left around.
        if not self.option("explain"):
            process.kill()

        raise self.failure_exception(msg)

    def _get_process_output(self, stream_readline):
        """ Returns the full process output from the given process stream """
        proc_lines = []
        while True:
            line = stream_readline()
            if not line:
                break
            proc_lines.append(line)
        return "".join(proc_lines)

    def _file_output_match_ratio(self, stream_readline, file):
        """ Returns the match ratio between a process and file """
        proc_lines = self._get_process_output(stream_readline)
        file_lines = pathlib.Path(file).resolve().read_text()
        return self._match_ratio(proc_lines, file_lines)

    def _string_output_match_ratio(self, stream_readline, string):
        """ Returns the match ratio between a process and string """
        proc_lines = self._get_process_output(stream_readline)
        return self._match_ratio(proc_lines, string)

    def _file_match_ratio(self, file1, file2):
        """ Returns the match ratio between two files """
        f1_lines = pathlib.Path(file1).resolve().read_text()
        f2_lines = pathlib.Path(file2).resolve().read_text()
        return self._match_ratio(f1_lines, f2_lines)

    def _match_ratio(self, expected_output, actual_output):
        """ Returns the match ratio between two strings """
        diff = difflib.SequenceMatcher(
            None, expected_output, actual_output, autojunk=False
        )
        ratio = diff.quick_ratio()
        return ratio

    def _compare_files(self, file1, file2, msg1=None, msg2=None, msg=None, type="file"):
        if not os.path.exists(file1):
            return msg1 or f"file missing: {file1}"
        elif not os.path.exists(file2):
            return msg2 or f"file missing: {file2}"
        else:
            # Files exist, so open and compare them
            f1 = open(file1, "rb")
            f2 = open(file2, "rb")

            different = False
            while True:
                b1 = f1.read(BUFFER_SIZE)
                b2 = f2.read(BUFFER_SIZE)

                if b1 != b2:
                    different = True
                    break

                if not b1:
                    # Reached end of file, and they are the same
                    break

            f1.close()
            f2.close()

            if different:
                msg = msg or f"{type} mismatch"
                if self.option("show_diff"):
                    # Add diff of output to message.
                    f1 = open(file1, "r")
                    f2 = open(file2, "r")
                    diff = difflib.unified_diff(
                        f1.readlines(), f2.readlines(), fromfile=file1, tofile=file2
                    )

                    msg += f"\nDiff leading to failure:\n{''.join(diff)}"

                    f1.close()
                    f2.close()
                return msg

    def _verbose_compare(self, stream_readline, file_path, stream_name, msg):
        if not os.path.exists(file_path):
            return f"file missing: {file_path}"
        else:
            # Files exist, so open and compare them
            different = False
            p_history = []
            f_history = []

            with open(file_path, "rb") as f:
                while True:
                    p_line = stream_readline()
                    f_line = f.readline().decode("utf-8")

                    # Store history of output.
                    p_history.append(p_line)
                    f_history.append(f_line)

                    if f_line != p_line:
                        different = True
                        break

                    if not f_line:
                        # Reached end of file, and they are the same
                        break

            if different:
                # Append diff to error message.
                diff = difflib.unified_diff(
                    p_history, f_history, fromfile=stream_name, tofile=file_path
                )
                msg = msg or ""
                msg += "\nDiff leading to failure [truncated]:\n"
                msg += "".join(diff)
                return msg

    def delay(self, secs):
        """Insert a delay into a test.
        Delay is in seconds, with fractions being acceptable.
        """
        if not self.option("explain"):
            time.sleep(secs)

    def fail(self, msg=None):
        """Fail immediately, with the given message."""
        if self.option("update"):
            # Ignore errors when in update mode.
            return

        raise self.failure_exception(msg)

    def assert_stdout_matches_file(self, process, file_path, msg=None):
        """
        Assert that the standard output of the process matches the
        contents of the given file.
        """
        if self.option("explain"):
            # Print out command to compare stdout.
            self._print_coloured(
                f"Compare stdout from Process {process.count}:", attrs=["bold"]
            )
            print(f"\tdiff {self._stdout_filename(process)} {file_path}")
            return
        if self.option("update") or self.option("save"):
            # Save stdout output to file.
            filename = file_path
            if self.option("save"):
                filename = self._stdout_filename(process)

            with open(filename, "wb") as f:
                while True:
                    line = process.readline_stdout()
                    f.write(line.encode())
                    if line == "":
                        break

        if self.option("update"):
            print(f"\tstandard output file updated: {file_path}")
            return

        result = None

        if self.option("save"):
            result = self._compare_files(
                self._stdout_filename(process), file_path, msg=msg, type="stdout"
            )
        elif self.option("show_diff"):
            result = self._verbose_compare(
                process.readline_stdout, file_path, self._stdout_filename(process), msg
            )
        elif self.option("fuzzy"):
            ratio = self._file_output_match_ratio(process.readline_stdout, file_path)
            if abs(ratio - 1.0) > 1e-03:
                result = f"stdout mismatch <<{round(ratio, 2)}>>"
        else:
            if not process.expect_stdout_file(file_path):
                result = "stdout mismatch"

        if result is not None:
            self._check_signal(process, result)

    def assert_stderr_matches_file(self, process, file_path, msg=None):
        """
        Assert that the standard error of the process matches the
        contents of the given file.
        """
        if self.option("explain"):
            # Print out command to compare stderr.
            self._print_coloured(
                f"Compare stderr from Process {process.count}:", attrs=["bold"]
            )
            print(f"\tdiff {self._stderr_filename(process)} {file_path}")
            return

        if self.option("update") or self.option("save"):
            # Save stderr output to file.
            filename = file_path
            if self.option("save"):
                filename = self._stderr_filename(process)

            with open(filename, "wb") as f:
                while True:
                    line = process.readline_stderr()
                    f.write(line.encode())
                    if line == "":
                        break

        if self.option("update"):
            print(f"\tstandard error file updated: {file_path}")
            return

        result = None

        if self.option("save"):
            result = self._compare_files(
                self._stderr_filename(process), file_path, msg=msg, type="stderr"
            )
        elif self.option("show_diff"):
            result = self._verbose_compare(
                process.readline_stderr, file_path, self._stderr_filename(process), msg
            )
        elif self.option("fuzzy"):
            ratio = self._file_output_match_ratio(process.readline_stderr, file_path)
            if abs(ratio - 1.0) > 1e-03:
                result = f"stderr mismatch <<{round(ratio, 2)}>>"
        else:
            if not process.expect_stderr_file(file_path):
                result = "stderr mismatch"

        if result is not None:
            self._check_signal(process, result)

    def assert_stdout(self, process, output, msg=None):
        """
        Assert that the standard output of the process contains the given
        output.
        """
        if self.option("explain"):
            # Print out the expected output from stdout.
            if output == "":
                self._print_coloured(
                    f"Expect end of file (Process {process.count} [stdout])",
                    attrs=["bold"],
                )
            else:
                self._print_coloured(
                    f"Expect output (Process {process.count} [stdout]): ",
                    attrs=["bold"],
                    end="",
                )
                print(safe_repr(output))
            return

        if self.option("update"):
            # Print message to remind user to check output
            # TODO: Include source code location
            print(f"\tCheck assert_stdout({safe_repr(output)})")
            return

        result = None
        if self.option("save"):
            # Save stdout and expected output to file.
            stdout_filename = self._stdout_filename(process)
            expected_filename = stdout_filename + ".expected"
            with open(stdout_filename, "wb") as f:
                while True:
                    line = process.readline_stdout()
                    f.write(line.encode())
                    if line == "":
                        break
            with open(expected_filename, "wb") as f:
                f.write(output.encode())

            result = self._compare_files(
                stdout_filename, expected_filename, msg=msg, type="stdout"
            )
        elif self.option("fuzzy"):
            ratio = self._string_output_match_ratio(process.readline_stdout, output)
            if abs(ratio - 1.0) > 1e-03:
                result = f"stdout mismatch <<{round(ratio, 2)}>>"
        else:
            if not process.expect_stdout(output):
                result = "stdout mismatch"

        if result is not None:
            self._check_signal(process, result)

    def assert_stderr(self, process, output, msg=None):
        """
        Assert that the standard error of the process contains the given
        output.
        """
        if self.option("explain"):
            # Print out the expected output from stdout.
            if output == "":
                self._print_coloured(
                    f"Expect end of file (Process {process.count} [stderr])",
                    attrs=["bold"],
                )
            else:
                self._print_coloured(
                    f"Expect output (Process {process.count} [stderr]): ",
                    attrs=["bold"],
                    end="",
                )
                print(safe_repr(output))
            return

        if self.option("update"):
            # Print message to remind user to check output
            # TODO: Include source code location
            print(f"\tCheck assert_stderr({safe_repr(output)})")
            return

        result = None
        if self.option("save"):
            # Save stderr and expected output to file.
            stderr_filename = self._stderr_filename(process)
            expected_filename = stderr_filename + ".expected"
            with open(stderr_filename, "wb") as f:
                while True:
                    line = process.readline_stderr()
                    f.write(line.encode())
                    if line == "":
                        break
            with open(expected_filename, "wb") as f:
                f.write(output.encode())

            result = self._compare_files(
                stderr_filename, expected_filename, msg=msg, type="stderr"
            )
        elif self.option("fuzzy"):
            ratio = self._string_output_match_ratio(process.readline_stderr, output)
            if abs(ratio - 1.0) > 1e-03:
                result = f"stderr mismatch <<{round(ratio, 2)}>>"
        else:
            if not process.expect_stderr(output):
                result = "stderr mismatch"

        if result is not None:
            self._check_signal(process, result)

    def assert_exit_status(self, process, status, msg=None):
        """
        Assert that the exit status of the process matches the given status.
        """
        if self.option("explain"):
            # Print out the expected exit status for the process.
            self._print_coloured(
                f"Expect exit status (Process {process.count}): ",
                attrs=["bold"],
                end="",
            )
            print(status)
            return

        if not process.assert_exit_status(status):
            msg = (
                msg
                or f"exit status mismatch: expected {status}, got {process.exit_status()}"
            )
            self._check_signal(process, msg)
    
    def assert_exit_status_not(self, process, status, msg=None):
        """
        Assert that the exit status of the process is not the given status.
        """
        if self.option("explain"):
            # Print out the unexpected exit status for the process.
            self._print_coloured(
                f"Expect exit status other than (Process {process.count}): ",
                attrs=["bold"],
                end="",
            )
            print(status)
            return

        if process.assert_exit_status(status):
            msg = (
                msg
                or f"exit status mismatch: did not expect {status}"
            )
            self._check_signal(process, msg)

    def assert_signalled(self, process, msg=None):
        """
        Assert that the process received a signal.
        """
        if self.option("explain"):
            # Print that the process is expected to receive a signal.
            self._print_coloured(
                f"Expect Process {process.count} to receive signal", attrs=["bold"]
            )
            return

        if not process.assert_signalled():
            msg = msg or "program did not receive signal"
            self._check_timeout(process, msg)

    def assert_signal(self, process, signal, msg=None):
        """
        Assert that the signal of the process matches the given signal.
        """
        if self.option("explain"):
            # Print out the expected signal for the process.
            self._print_coloured(
                f"Expect signal (Process {process.count}): ", attrs=["bold"], end=""
            )
            print(signal)
            return

        if not process.assert_signal(signal):
            msg = msg or f"signal mismatch: expected {signal}, got {process.signal}"
            self._check_timeout(process, msg)

    def assert_files_equal(self, file, expected, msg=None):
        """
        Assert that the given files contain exactly the same contents.
        """
        if self.option("explain"):
            # Print out the command to check the two files.
            self._print_coloured("Check files are the same:", attrs=["bold"])
            print(f"\tdiff {file} {expected}")
            return

        result = None

        if self.option("update"):
            print(f"\texpected file updated: {expected}")
            shutil.copyfile(file, expected)
            return
            

        if self.option("fuzzy"):
            ratio = self._file_match_ratio(file, expected)
            if abs(ratio - 1.0) > 1e-03:
                result = f"file mismatch <<{round(ratio, 2)}>>"
        else:
            result = self._compare_files(file, expected, msg=msg)

        if result is not None:
            raise self.failure_exception(result)

    def add_detail(self, name, data):
        """Record information related to the test"""
        self.__details[name] = data

    def _process_details(self, result):
        """Update details in the result with those stored from the test."""
        result.update_details(self.__details)

    def child_pids(self, parent):
        """Get the process IDs of the children of the given parent process."""
        pids = []
        if self.option("explain"):
            self._print_coloured(
                f"Get IDs of child processes of Process {parent.count}", attrs=["bold"]
            )
        elif isinstance(parent, xTracedProcess):
            pids = parent.child_pids()
        else:
            pgrep = self.process(["pgrep", "-P", str(parent.pid)])
            while True:
                pid = pgrep.readline_stdout()
                if pid == "":
                    break
                pid = int(pid.strip())
                pids.append(pid)
            pgrep.assert_exit_status(0)
            del pgrep
        return pids

    def signal_process(self, pid, sig, explain_process=None):
        """Send a signal to the process with the given ID."""
        if self.option("explain"):
            proc = explain_process or "a process (determined at runtime)"
            msg = f"Send signal {sig} to {proc}"
            self._print_coloured(msg, attrs=["bold"])
        else:
            try:
                os.kill(pid, sig)
            except:
                self.fail(f"Failed to send signal {sig} process {pid}")

    def explain(self, msg, fg=None, bg=None, attrs=None, **kwargs):
        """
        Print a message when the test is run in explain mode.
        Message can include colour output and other formatting, which will be
        displayed if the output location is a TTY.
        """
        if self.option("explain"):
            self._print_coloured(msg, fg, bg, attrs, **kwargs)
