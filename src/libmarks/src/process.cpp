#include <iostream>
#include <fstream>
#include <string>
#include <vector>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <csignal>
#include <unistd.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <pthread.h>
#include <boost/shared_ptr.hpp>

#include "process.hpp"

/* Allow global value for LD_PRELOAD to be set for all Processes created */
namespace {
    std::string preload_value = "";
}

void set_ld_preload(std::string value)
{
    preload_value = value;
}

std::string get_ld_preload()
{
    return preload_value;
}

/* Use to create a new Process, so init() is called */
boost::shared_ptr<Process> create_process(std::vector<std::string> argv, std::string inputFile)
{
    boost::shared_ptr<Process> p(new Process(argv, inputFile));
    p->init();
    return p;
}

/* Use to create a new TimeoutProcess, so init() is called */
boost::shared_ptr<TimeoutProcess> create_timeout_process(std::vector<std::string> argv, int timeout, std::string inputFile)
{
    boost::shared_ptr<TimeoutProcess> p(new TimeoutProcess(argv, timeout, inputFile));
    p->init();
    return p;
}

/* Helper to release the GIL */
class ScopedGILRelease
{
public:
    inline ScopedGILRelease()
    {
        m_thread_state = PyEval_SaveThread();
    }

    inline ~ScopedGILRelease()
    {
        PyEval_RestoreThread(m_thread_state);
        m_thread_state = NULL;
    }

private:
    PyThreadState * m_thread_state;
};


/* Public */
Process::Process(std::vector<std::string> argv, std::string inputFile):
    argv(argv), inputFile(inputFile),
    input(NULL), output(NULL), error(NULL), finished(false),
    abnormalExit(false), signalled(false), timeout(false)
{
    signalNum = -1;
    exitStatus = -1;
}

Process::Process(std::vector<std::string> argv):
    argv(argv), inputFile(""),
    input(NULL), output(NULL), error(NULL), finished(false),
    abnormalExit(false), signalled(false), timeout(false)
{
    signalNum = -1;
    exitStatus = -1;
}

Process::~Process()
{
    if (!finished) {
        try {
            send_kill();
        } catch (SignalException& e) {
            // Nothing can be done at this point, so ignore.
        }
    }
    close_stream(&output);
    close_stream(&error);
    // Destroy mutex.
    pthread_mutex_destroy(&finishMutex);
}

pid_t Process::get_pid()
{
    return childPid;
}

bool Process::send(const std::string& message)
{
    if (input == NULL) {
        // No input pipe (pipe was not created, as input file was specified).
        return false;
    }

    int count = fprintf(input, "%s", message.c_str());
    if (count == -1 || (unsigned int) count != message.length()) {
        // Sending failed.
        return false;
    }

    // Ensure input is sent to child.
    fflush(input);

    // Full message was sent.
    return true;
}

bool Process::send_file(char *filePath)
{
    if (input == NULL) {
        // No input pipe (pipe was not created, as input file was specified).
        return false;
    }

    std::ifstream data (filePath);

    // Write the contents of the file to the program.
    data.close();
    return false;
}

bool Process::finish_input()
{
    if (input == NULL) {
        // No input pipe (pipe was not created, as input file was specified).
        return false;
    }

    // Obtain mutex.
    pthread_mutex_lock(&finishMutex);

    bool result = close_stream(&input);

    // Release mutex.
    pthread_mutex_unlock(&finishMutex);

    return result;
}

bool Process::expect_stdout(const std::string& expected)
{
    return expect(expected, &output);
}

bool Process::expect_stderr(const std::string& expected)
{
    return expect(expected, &error);
}

bool Process::expect_stdout_file(char *filePath)
{
    return expect_file(filePath, &output);
}

bool Process::expect_stderr_file(char *filePath)
{
    return expect_file(filePath, &error);
}

std::string Process::readline_stdout()
{
    return readline(&output);
}

std::string Process::readline_stderr()
{
    return readline(&error);
}

void Process::print_stdout()
{
    print_stream(&output);
}

void Process::print_stderr()
{
    print_stream(&error);
}

bool Process::assert_exit_status(int expected)
{
    if (!finished)
        perform_wait(true);
    return !abnormalExit && exitStatus == expected;
}

bool Process::assert_signalled(bool expected)
{
    if (!finished)
        perform_wait(true);
    return signalled == expected;
}

bool Process::assert_signal(int expected)
{
    if (!finished)
        perform_wait(true);
    return signalled && signalNum == expected;
}

int Process::get_exit_status()
{
    return exitStatus;
}

bool Process::get_abnormal_exit()
{
    return abnormalExit;
}

bool Process::get_signalled()
{
    return signalled;
}

int Process::get_signal()
{
    return signalNum;
}

/**
 * Send a signal to the child process.
 * @param signalVal The signal to send to the process.
 */
void Process::send_signal(int signalVal)
{
    if (!finished) {
        // TODO: Check range allowed for child pid
        // negated pid so we kill the process group
        if (childPid <= 0 || kill(-1 * childPid, SIG_CHECK) == -1) {
            // Perform a wait, as the group may be dead already.
            perform_wait(true);
            if (!finished)
                throw SignalException();
        } else if (kill(childPid, signalVal) == -1) {
            throw SignalException();
        }
    }
}

/**
 * Send a signal to the process group of the child.
 * @param signalVal The signal to send to the process group.
 */
void Process::send_signal_group(int signalVal)
{
    // TODO: Check range allowed for child pid
    if (childPid <= 0 || kill(-childPid, SIG_CHECK) == -1) {
        // Perform a wait, as the group may be dead already.
        perform_wait(true);
        if (!finished)
            throw SignalException();
    } else if (kill(-childPid, signalVal) == -1) {
        throw SignalException();
    }
}

/**
 * Send SIGKILL to the entire process group of the child.
 */
void Process::send_kill()
{
    send_signal_group(SIGKILL);
    perform_wait(true);
}

/**
 * Check if the child process was signalled, via a non-blocking call to
 * `waitpid`.
 * @return true if process is finished and was signalled, false otherwise.
 */
bool Process::check_signalled()
{
    perform_wait(false);
    return finished && signalled;
}

bool Process::get_timeout()
{
    return timeout;
}

/* Private */
void Process::init()
{
    // Create pipe for stdin only if there is no input file.
    if (inputFile.empty() && pipe(fdIn) != 0) {
        throw PipeException();
    }

    // Create the remaining pipes.
    if (pipe(fdOut) != 0 || pipe(fdErr) != 0 || pipe(fdCheck) != 0) {
        throw PipeException();
    }

    // Initialise mutex for finishing a process.
    pthread_mutex_init(&finishMutex, NULL);

    // Fork
    childPid = fork();

    if (childPid < 0) {
        char buff[100];
        buff[0]='\0';
        strerror_r(errno, buff, 99);
        throw ForkException(std::string(buff));
    } else if (childPid == 0) {
        // Child process.
        setup_child();
    } else {
        // Parent process.
        setup_parent();
    }
}

void Process::setup_parent()
{
    // Close the ends of the pipes that are being used in the child.
    if ((inputFile.empty() && close(fdIn[READ]) == -1) ||
            close(fdOut[WRITE]) == -1 || close(fdErr[WRITE]) == -1 ||
            close(fdCheck[WRITE]) == -1) {
        throw CloseException();
    }

    if (setup_parent_pre_exec() == -1)
        throw ExecException(); // TODO: Change this exception

    // Attempt to read on check pipe. If data is available, then exec failed.
    char buf[5];
    if (read(fdCheck[READ], buf, 5) != 0) {
        perform_wait(true);
        throw ExecException();
    }

    // Close the check pipe, now we are finished with it.
    if (close(fdCheck[READ]) == -1)
        throw CloseException();

    // Open child stdin as a file, if pipe was created.
    if (inputFile.empty()) {
        input = fdopen(fdIn[WRITE], "w"); // Input from parent to child.
    }

    // Open the remaining pipes as files, for ease of use.
    output = fdopen(fdOut[READ], "r"); // stdout from child to parent.
    error = fdopen(fdErr[READ], "r"); // stderr from child to parent.

    if ((inputFile.empty() && input == NULL) || output == NULL || error == NULL) {
        throw FdOpenException();
    }
}

int Process::setup_parent_pre_exec()
{
    /* No additional setup required */
    return 1;
}

void Process::setup_child()
{
    char **args;
    int fd = -1, flags = 0;

    // Set up child input (stdin)
    if (inputFile != "") {
        // Open file as stdin for child.
        fd = open(inputFile.c_str(), O_RDONLY);
        if (fd == -1 || dup2(fd, STDIN_FILENO) == -1 || close(fd) == -1) {
            D("Failed to open file as stdin (" << inputFile << ")" << std::endl);
            goto childerror;
        }
    } else {
        // Finish pipe creation.
        if (close(fdIn[WRITE]) == -1 ||
                dup2(fdIn[READ], STDIN_FILENO) == -1 ||
                close(fdIn[READ]) == -1) {
            D("Failed to setup pipe for stdin" << std::endl);
            goto childerror;
        }
    }

    // Set up child output (stdout).
    if (close(fdOut[READ]) == -1 ||
            dup2(fdOut[WRITE], STDOUT_FILENO) == -1 ||
            close(fdOut[WRITE]) == -1) {
        D("Failed to setup pipe for stdout" << std::endl);
        goto childerror;
    }

    // Set up child error (stderr).
    if (close(fdErr[READ]) == -1 ||
            dup2(fdErr[WRITE], STDERR_FILENO) == -1 ||
            close(fdErr[WRITE]) == -1) {
        D("Failed to setup pipe for stderr" << std::endl);
        goto childerror;
    }

    // Set up check pipe.
    if (close(fdCheck[READ]) == -1)
        goto childerror;

    // Setup new progress group with this process as leader.
    if (setpgid(0, 0) == -1)
        goto childerror;

    // Set up close-on-exec for the check pipe.
    flags = fcntl(fdCheck[WRITE], F_GETFD);
    if (flags == -1)
        goto childerror;

    flags |= FD_CLOEXEC;

    if (fcntl(fdCheck[WRITE], F_SETFD, flags) == -1)
        goto childerror;

    if (!preload_value.empty()) {
        D("Setting LD_PRELOAD for child: " << preload_value << std::endl);
#ifdef __APPLE__
        if (setenv("DYLD_FORCE_FLAT_NAMESPACE", "1", 1) == -1 ||
                setenv("DYLD_INSERT_LIBRARIES", preload_value.c_str(), 1) == -1)
            goto childerror;
#else
        if (setenv("LD_PRELOAD", preload_value.c_str(), 1) == -1)
            goto childerror;
#endif
    } else {
        D("LD_PRELOAD not set - value empty" << std::endl);
    }

    // Perform additional child setup, as possibly defined by subclasses.
    if (setup_child_additional() == -1)
        goto childerror;

    // Execute the program.
    args = create_args(argv);
    execvp(args[0], args);
    delete_args(args, argv.size());

childerror:
    // Exec failed if program reaches this point.
    write(fdCheck[WRITE], "fail", 4);
    close(fdCheck[WRITE]);
    exit(-1);
}

int Process::setup_child_additional()
{
    /* No additional setup */
    return 1;
}

char **Process::create_args(std::vector<std::string> &argv)
{
    // Create args array, including space for the NULL array terminator.
    char **args = new char*[argv.size() + 1];

    // Copy the args to the new array.
    for(size_t i = 0; i < argv.size(); i++){
        args[i] = new char[argv[i].length() + 1];
        strcpy(args[i], argv[i].c_str());
    }

    // argv array for exec* needs to be null terminated.
    args[argv.size()] = NULL;

    return args;
}

void Process::delete_args(char **args, size_t length)
{
    // Count to length + 1, to accommodate NULL array terminator.
    for (unsigned int i = 0; i < length + 1; ++i) {
        delete [] args[i];
    }
    delete [] args;
}

bool Process::expect_file(char *filePath, FILE **stream)
{
    std::ifstream expectedOutput (filePath);

    if (!expectedOutput.is_open()) {
        throw StreamException();
    }

    if (*stream == NULL) {
        // Trying to access a stream after the process has finished.
        throw StreamFinishedException();
    }

    char expected, received;

    // Char by char, check expected output against received output.
    do {
        expected = expectedOutput.get();
        if (*stream == NULL) // Stream may have been closed by timeout.
            return false;
        received = fgetc(*stream);
    } while (expectedOutput.good() && received != EOF && expected == received);

    // If output was same as expected, then both should be at end of file.
    if (expectedOutput.eof() && feof(*stream)) {
        expectedOutput.close();
        return true;
    }

    expectedOutput.close();
    return false;
}

bool Process::expect(const std::string& expected, FILE **stream)
{
    if (*stream == NULL) {
        // Trying to access a stream after the process has finished.
        throw StreamFinishedException();
    }

    if (expected.length() == 0) {
        // Expected string is 0 length, so expect EOF to be returned.
        if (*stream == NULL || fgetc(*stream) != EOF) {
            return false;
        }
    } else {
        // Check each char in the expected against chars in the stream.
        char c;
        for (unsigned int i = 0; i < expected.length(); ++i) {
            if (*stream == NULL || (c = fgetc(*stream)) == EOF ||
                    expected[i] != c) {
                return false;
            }
        }
    }

    return true;
}

/**
 * Read a line from a stream, stopping when a new line character or end of file
 * is reached.
 * An empty string indicates end of file was reached.
 * @param  stream The stream to read from.
 * @return        The line read from the stream, including the newline character.
 */
std::string Process::readline(FILE **stream)
{
    if (*stream == NULL) {
        // Trying to access a stream after the process has finished.
        throw StreamFinishedException();
    }

    std::string line;
    char c;

    while(*stream != NULL && (c = fgetc(*stream)) != EOF) {
        line += c;
        if (c == '\n') {
            break;
        }
    }

    return line;
}

void Process::print_stream(FILE **stream)
{
    if (*stream == NULL) {
        // Trying to access a stream after the process has finished.
        throw StreamFinishedException();
    }

    char *buf = new char[80];

    while (*stream != NULL && !feof(*stream)) {
        if (fgets(buf, 80, *stream) == NULL)
            break;
        std::cout << buf;
    }

    delete [] buf;
}

bool Process::close_stream(FILE **stream)
{
    if (*stream != NULL) {
        int status = fclose(*stream);

        // Mark the stream as being closed.
        *stream = NULL;

        if (status != 0) {
            return false;
        }
    }

    return true;
}

void Process::finish_process(int status)
{
    // Obtain mutex.
    pthread_mutex_lock(&finishMutex);

    // Check for the exit status of the child.
    if (WIFEXITED(status)) {
        exitStatus = WEXITSTATUS(status);
    } else {
        abnormalExit = true;
    }

    // Check signal, if it was signalled.
    if (WIFSIGNALED(status)) {
        signalled = true;
        signalNum = WTERMSIG(status);
    }

    // Close the files.
    close_stream(&input);
    //close_stream(&output);
    //close_stream(&error);

    finished = true;

    // Release mutex.
    pthread_mutex_unlock(&finishMutex);
}

void Process::perform_wait(bool block)
{
    if (!finished) {
        // Set up options for waitpid, based on whether we should wait
        // for the process to complete or not.
        int options = 0;
        if (!block) {
            options = WNOHANG;
        }

        // Wait on the child and reap it once it is complete.
        int status;
        int result = waitpid(childPid, &status, options);

        if (result == -1) {
            // Error detected - Child is already finished, or call was
            // interrupted by caught signal.
        } else if (!block && result == 0) {
            // Child is not finished, so do not check status.
        } else {
            // Child is finished, so check status and close streams.
            finish_process(status);
        }
    }
}

/** Timeout Process **/
/* Public */
TimeoutProcess::TimeoutProcess(std::vector<std::string> argv, int timeout, std::string inputFile):
    Process(argv, inputFile), timeout_duration(timeout), timeoutStarted(false)
{}

TimeoutProcess::TimeoutProcess(std::vector<std::string> argv, int timeout):
    Process(argv, ""), timeout_duration(timeout), timeoutStarted(false)
{}

TimeoutProcess::~TimeoutProcess()
{
    if (timeoutStarted) {
        // Finish the timeout thread.
        pthread_cancel(timeoutThread);
        pthread_join(timeoutThread, NULL);
    }
}

void TimeoutProcess::init()
{
    Process::init();
    init_timeout();
}

int TimeoutProcess::get_timeout_duration()
{
    return timeout_duration;
}

void TimeoutProcess::perform_timeout()
{
    if (!finished) {
        timeout = true;
        try {
            send_kill();
        } catch (SignalException& e) {
            // Ignore exception here - possible that children already dead.
        }
    }
}

/* Private */
void TimeoutProcess::init_timeout()
{
    // Create thread to perform timeout
    if (pthread_create(&timeoutThread, NULL, &TimeoutProcess::timeout_thread,
            (void *) this) != 0) {
        // Error
        return;
    }
    timeoutStarted = true;
}

void TimeoutProcess::timeout_process()
{
    // Sleep for timeout length
    sleep(timeout_duration);

    // If process is not finished, kill process
    perform_timeout();
}

/* Timeout thread */
void *TimeoutProcess::timeout_thread(void *arg)
{
    TimeoutProcess *tp = (TimeoutProcess *) arg;
    tp->timeout_process();

    // End thread
    pthread_exit(NULL);
}
