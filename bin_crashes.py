#!/usr/bin/python
#
# Process crashes to determine uniqueness
#
# Based on: 
#   Framework for fuzzing things
#   author: Chris Bisnett

import glob
import os
import sys
import time
import subprocess
import signal

from ptrace.debugger.debugger import PtraceDebugger
from ptrace.debugger.child import createChild
from ptrace.debugger.process_event import ProcessExit
from ptrace.debugger.ptrace_signal import ProcessSignal

from multiprocessing import Process, Queue

import framework


# This is a Queue that behaves like stdout
class StdoutQueue():
    def __init__(self,*args,**kwargs):
        self.q = args[0]

    def write(self,msg):
        self.q.put(msg)

    def flush(self):
        pass


# this is the child process of bin_crashes
# it will start the server as his own child
# it will communicate crash report of the child (server) to the parent
def startServer(config, queue_sync, queue_out):
    # Sadly this does not apply to child processes started via
    # createChild(), so we can only capture output of this python process
    stdoutQueue = StdoutQueue(queue_out)
    #sys.stdout = stdoutQueue
    #sys.stderr = stdoutQueue

    queue_out.put("Dummy")
    # do not remove print, parent excepts something
    print "Start Server"
    #sys.stderr.write("Stderr")

    # create child via ptrace debugger
    # API: createChild(arguments[], no_stdout, env=None)
    pid = createChild(
        [
            config["target_bin"],
            str(config["target_port"]),
        ],
        False,
        None,
    )

    # Attach to the process with ptrace and let it run
    dbg = PtraceDebugger()
    proc = dbg.addProcess(pid, True)
    proc.cont()

    # notify parent about the pid
    queue_sync.put( ("pid", pid) )

    event = None
    while True:
        print "T: Wait for process"
        event = dbg.waitProcessEvent()

        # If this is a process exit we need to check if it was abnormal
        if type(event) == ProcessExit:
            if event.signum is None or event.exitcode == 0:
                # Clear the event since this was a normal exit
                event = None

        # If this is a signal we need to check if we're ignoring it
        elif type(event) == ProcessSignal:
            if event.signum == signal.SIGCHLD:
                # Ignore these signals and continue waiting
                continue
            #elif event.signum == signal.SIGTERM:
            #    event = None

        break

    # send crash details
    # Note: If the server does not crash, we kill it in the parent.
    #       This will still generate a (unecessary) "crash" message and will be sent here
    # TODO fixme maybe
    if event is not None and event.signum != 15: 
        print "T: Crash"
        data = getCrashDetails(event)
        queue_sync.put( ("data", data) )
    else: 
        data = ()
        print "T: NO crash"
        queue_sync.put( ("data", data) )

    dbg.quit()



def getAsanOutput(config, pid): 
    # as we cannot get stdout/stderr of child process, we store asan
    # output in the temp folder in the format: asan.<pid>
    fileName = config["temp_dir"] + "/asan." + str(pid)
    print "Get asan output"
    print "  filename: " + str(fileName)

    time.sleep(0.5) # omg wait for the file to appear

    if not os.path.isfile(fileName):
        print "  !!!!!!!!! NO ASAN OUTPUT !!!"
        return "  !!!!!!!!! NO ASAN OUTPUT !!!"

    # it may not exist, which aint bad (e.g. no asan support)
    file = open(fileName, "r")
    data = file.read()
    print "  Got ASAN OUTPUT: " + str(len(data))
    file.close()

    # remove the file, as we dont need it anymore
    os.remove(fileName)

    return data


def minimize(config):
    print "Crash minimize"
    # Tell Glibc to abort on heap errors but not dump a bunch of output
    os.environ["MALLOC_CHECK_"] = "2"

    queue_sync = Queue()
    queue_out = Queue()
    crashes = dict()
    n = 100

    outcomesDir = os.path.abspath(config["outcome_dir"])
    outcomes = glob.glob(os.path.join(outcomesDir, '*.raw'))

    framework._setupEnvironment(config)
    print "Processing %d outcomes" % len(outcomes)

    for outcome in outcomes:
        print "\n\n Now: " + str(n) + ": " + outcome
        config["target_port"] = config["baseport"] + n 
        n += 1

        # start server in background
        p = Process(target=startServer, args=(config, queue_sync, queue_out))
        p.start()

        # wait for ok (pid) from child that the server has started
        data = queue_sync.get()
        serverPid = data[1]
        print "M: Server pid: " + str(serverPid)
        time.sleep(1) # wait a bit till server is ready
        while not framework.testServerConnection(config):
            print "Server not ready, waiting and retrying"
            time.sleep(0.1) # wait a bit till server is ready
        
        print "M: Send"
        framework.sendDataToServer(config, outcome)

        # get crash result data
        # or empty if server did not crash
        try:
            print "M: Wait for crash data"
            crashData = queue_sync.get(True, 1)
            print "M: Crash!"
            crashData = crashData[1]
            crashOutput = queue_out.get()
            asanOutput = getAsanOutput(config, serverPid)
            details = crashData[3]
            signature = ( crashData[0], crashData[1], crashData[2] )
            details = {
                "faultOffset": crashData[0],
                "module": crashData[1],
                "signature": crashData[2],
                "gdbdetails": crashData[3],
                "output": crashOutput,
                "asan": asanOutput,
                "file": outcome,
            }
            crashes[signature] = details
            storeValidCrash(config, signature, details)
        except Exception as error:
            print "M: Waited long enough, NO crash. "
            print "Exception: " + str(error)
            # timeout waiting for the data, which means the server did not crash
            # kill it, and receive the unecessary data
            # TODO: If os.kill throws an exception, it could not kill it, therefore
            #       the start of the server failed. Retry
            try: 
                os.kill(serverPid, signal.SIGTERM)
            except:
                print "  M: !!!!!!!!!!! Exception: Could not kill :-("

            try: 
                notneeded1 = queue_sync.get(True, 1)
                crashOutput = queue_out.get(True, 1)
            except:
                print "  M: !!!!!!!!!!! Exception: No data to get for non-crash :-("


        # wait for child to exit
        p.join()

    # manage all these crashes
    for crash in crashes:
        offset, mod, sig = crash
        details = crashes[crash]
        print "Crash: %s+0x%x (signal %d)" % (mod, offset, sig)
        print "\t%s" % details["gdbdetails"]


def storeValidCrash(config, crashSig, crashDetail):
    with open(os.path.join(config["outcome_dir"], crashDetail["file"] + ".crashdata.txt"), "w") as f:
        f.write("Offset: %s\n" % crashDetail["faultOffset"])
        f.write("Module: %s\n" % crashDetail["module"])
        f.write("Signature: %s\n" % crashDetail["signature"])
        f.write("Details: %s\n" % crashDetail["gdbdetails"])
        f.write("Time: %s\n" % time.strftime("%c"))
        f.write("Output:\n %s\n" % crashDetail["output"])
        f.write("\n")
        f.write("ASAN Output:\n %s\n" % crashDetail["asan"])
        f.close()


def getCrashDetails(event):
    # Get the address where the crash occurred
    faultAddress = event.process.getInstrPointer()

    # Find the module that contains this address
    # Now we need to turn the address into an offset. This way when the process
    # is loaded again if the module is loaded at another address, due to ASLR,
    # the offset will be the same and we can correctly detect those as the same
    # crash
    module = None
    faultOffset = 0
    for mapping in event.process.readMappings():
        if faultAddress >= mapping.start and faultAddress < mapping.end:
            module = mapping.pathname
            faultOffset = faultAddress - mapping.start
            break

    # Apparently the address didn't fall within a mapping
    if module is None:
        module = "Unknown"
        faultOffset = faultAddress

    # Get the signal
    sig = event.signum

    # Get the details of the crash
    details = None
    if event._analyze() is not None:
        details = event._analyze().text

    return (faultOffset, module, sig, details)
