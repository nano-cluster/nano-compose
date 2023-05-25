#! /usr/bin/python -u

import sys
import os
import time
import json
import ctypes
import ctypes.util
import select
import typing
import shlex
import signal
import yaml

from collections import defaultdict

stats_balance_mothod = defaultdict(int)
stats_balance_mothod_caller = defaultdict(int)
stats_balance_inter_mod = defaultdict(int)
stats_total_at = 0
stats_total_mothod = defaultdict(int)
stats_total_mothod_caller = defaultdict(int)
stats_total_inter_mod = defaultdict(int)
stats_err_at = 0
stats_err_mothod = defaultdict(int)
stats_err_mothod_caller = defaultdict(int)
stats_err_inter_mod = defaultdict(int)
stats = {
    "balance_mothod": stats_balance_mothod,
    "balance_mothod_caller": stats_balance_mothod_caller,
    "balance_inter_mod": stats_balance_inter_mod,
    "total_at": stats_total_at,
    "total_mothod": stats_total_mothod,
    "total_mothod_caller": stats_total_mothod_caller,
    "total_inter_mod": stats_total_inter_mod,
    "err_at": stats_err_at,
    "err_mothod": stats_err_mothod,
    "err_mothod_caller": stats_err_mothod_caller,
    "err_inter_mod": stats_err_inter_mod,
}


STDIN_FILENO, STDOUT_FILENO, STDERR_FILENO = (0,1,2)

# more details in unistd.h
# although the functions we want are in the kernel not libc
libc = ctypes.CDLL(ctypes.util.find_library("c"))
# man dup2(2): int dup2(int oldfd, int newfd);
libc.dup2.argtypes = (ctypes.c_int, ctypes.c_int)
libc.dup2.restype = ctypes.c_int
# man close(2): int close(int fd);
libc.close.argtypes = (ctypes.c_int,)
libc.close.restype = ctypes.c_int
# man prctl(2): int prctl(int option, int arg2);
if sys.platform=='linux':
    libc.prctl.argtypes = (ctypes.c_int, ctypes.c_long)
    libc.prctl.restype = ctypes.c_int

PR_SET_PDEATHSIG = 1

def log(*msgs, sep=" ", end="\n"):
    """similar to print, but uses stderr"""
    line = (sep.join(["{}".format(msg) for msg in msgs]))+end
    sys.stderr.write(line)
    sys.stderr.flush()

class NanoCompose:
    def __init__(self):
        self.modules = {
            "_admin": {"uses": set(), "only_from": None, "desc": {}},
        }
        self.r = {}
        self.w = {}
        self.pids = []
        self.reader_poll = select.poll()
        self.reader_files = {}
        self.pending_ids = {}


def child_std_fd(cr, cw):
    """
    make child read fd to be stdin
    make child write fd to be stdout
    """
    # make client-read to be stdin
    libc.close(STDIN_FILENO)
    libc.dup2(cr, STDIN_FILENO)
    libc.close(cr)
    # do the same with client write
    libc.close(STDOUT_FILENO)
    libc.dup2(cw, STDOUT_FILENO)
    libc.close(cw)
    return STDIN_FILENO, STDOUT_FILENO


def run_module_child(nano_compose, module_name, module_desc, cr, cw):
    child_std_fd(cr, cw)
    # TODO: if container with build/image exec podman
    fork_cmd = module_desc["fork"]
    args = shlex.split(fork_cmd)
    return os.execl(args[0], *args)



def run_module(nano_compose, module_name, module_desc):
    # consider (cr, pw) = multiprocessing.Pipe() # this might involve pickle
    cr, pw = os.pipe2(0)
    pr, cw = os.pipe2(0)
    pid = os.fork()
    if pid==0:
        # when parent dies send SIGHUP to child
        # SIGHUP: Hangup detected on controlling terminal or death of controlling process
        if sys.platform=='linux':
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGHUP.value);
        return run_module_child(nano_compose, module_name, module_desc, cr, cw)
    only_from = set(module_desc["only_from"] or []) if "only_from" in module_desc else None
    nano_compose.modules[module_name] = {
        "pid": pid,
        "r": pr,
        "w": pw,
        "uses": set(module_desc.get("uses") or []),  # default empty
        "only_from": only_from,  # default None
        "desc": module_desc
    }
    nano_compose.pids.append(pid)
    nano_compose.r[pr] = module_name
    nano_compose.w[pw] = module_name
    nano_compose.reader_poll.register(pr) # select.POLLIN | select.POLLPRI | select.POLLERR | select.POLLHUP | select.POLLRDHUP
    nano_compose.reader_files[pr] = os.fdopen(pr, 'rb')
    return pid

def can_invoke(module_name_from, module_name_to, from_uses, to_only_from):
    if module_name_to not in from_uses:
        return False
    return to_only_from is None or module_name_from in to_only_from

def stats_delta(module_name_caller, module_name_callee, method, val, err=0):
    global stats_total_at, stats_err_at
    stats_balance_mothod[method] += val
    stats_balance_mothod_caller[module_name_caller+":"+method] += val
    stats_balance_inter_mod[module_name_caller+":"+module_name_callee] += val
    if val>0:
        stats_total_at = time.time()
        stats_total_mothod[method] += 1
        stats_total_mothod_caller[module_name_caller+":"+method] += 1
        stats_total_inter_mod[module_name_caller+":"+module_name_callee] += 1
    if err:
        stats_err_at = time.time()
        stats_err_mothod[method] += err
        stats_err_mothod_caller[module_name_caller+":"+method] += err
        stats_err_inter_mod[module_name_caller+":"+module_name_callee] += err

def admin_invoke(parsed):
    method = parsed["method"]
    id = parsed["id"]
    if method!="_admin.get_stats":
        res_parsed = {"id": id, "error":{
            "codename": "xrpc.forbidden",
            "message": f"unknown method {method}",
        }}
    res_parsed = {"id": id, "result": stats}
    return res_parsed

def invoke(nano_compose, module_name_caller, line, parsed):
    method = parsed["method"]
    id = parsed["id"]
    params = parsed["params"]
    parts = method.split(".", 1)
    module_name_callee = parts[0]
    stats_delta(module_name_caller, module_name_callee, method, 1)
    mod_caller = nano_compose.modules[module_name_caller]
    mod_callee = nano_compose.modules[module_name_callee]
    from_uses = mod_caller["uses"]
    to_only_from = mod_callee["only_from"]
    nano_compose.pending_ids[id] = (module_name_caller, method)
    if not can_invoke(module_name_caller, module_name_callee, from_uses, to_only_from):
        res_parsed = {"id": id, "error":{
            "codename": "xrpc.forbidden",
            "message": f"module {module_name_caller} is not allowed to call {module_name_callee}",
        }}
        log("forbidden: ", res_parsed)
        res_line = json.dumps(res_parsed, ensure_ascii=False).encode("utf-8")+b"\n"
        pass_result(nano_compose, module_name_callee, res_line, res_parsed)
        return
    if module_name_callee == "_admin":
        res_parsed = admin_invoke(parsed)
        res_line = json.dumps(res_parsed, ensure_ascii=False).encode("utf-8")+b"\n"
        return pass_result(nano_compose, module_name_callee, res_line, res_parsed)
    os.write(mod_callee["w"], line)
    return



def pass_result(nano_compose, module_name_callee, line, parsed):
    id = parsed["id"]
    module_name_to, method = nano_compose.pending_ids[id]
    with_err = 1 if "error" in parsed else 0
    stats_delta(module_name_to, module_name_callee, method, -1, with_err)
    os.write(nano_compose.modules[module_name_to]["w"], line)
    del nano_compose.pending_ids[id]

def handle_one(nano_compose, module_name_from, line, parsed):
    if "method" in parsed:
        return invoke(nano_compose, module_name_from, line, parsed)
    return pass_result(nano_compose, module_name_from, line, parsed)

def main():
    filename = "nano_compose.yaml"
    with open(filename, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f) or {}
    nano_compose = NanoCompose()
    module_by_name = content["modules"]
    for module_name, module_desc in module_by_name.items():
        log(f"running module [{module_name}]: ...")
        pid = run_module(nano_compose, module_name, module_desc)
        log(f"running module [{module_name}]: pid={pid}")
    log(f"waiting: ...")
    while True:
        ls = nano_compose.reader_poll.poll()
        for fd, event in ls:
            if event != select.POLLIN and event != select.POLLPRI:
                continue
            module_name_from = nano_compose.r[fd]
            f: typing.IO = nano_compose.reader_files[fd]
            line = f.readline()
            parsed = json.loads(line)
            handle_one(nano_compose, module_name_from, line, parsed)
    os.wait()

if __name__ == "__main__":
    main()
