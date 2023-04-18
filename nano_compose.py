#! /usr/bin/python -u

import sys
import os
import time
import json
import ctypes
import ctypes.util
import select
import typing

import yaml

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

def log(*msgs, sep=" ", end="\n"):
    """similar to print, but uses stderr"""
    line = (sep.join(["{}".format(msg) for msg in msgs]))+end
    sys.stderr.write(line)
    sys.stderr.flush()

class NanoCompose:
    def __init__(self):
        self.modules = {}
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
    return os.execl(module_desc["fork"], module_desc["fork"])

def run_module(nano_compose, module_name, module_desc):
    # consider (cr, pw) = multiprocessing.Pipe() # this might involve pickle
    cr, pw = os.pipe2(0)
    pr, cw = os.pipe2(0)
    pid = os.fork()
    if pid==0:
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


def invoke(nano_compose, module_name_from, line, parsed):
    method = parsed["method"]
    id = parsed["id"]
    params = parsed["params"]
    parts = method.split(".", 1)
    module_name_to = parts[0]
    mod_from = nano_compose.modules[module_name_from]
    mod_to = nano_compose.modules[module_name_to]
    from_uses = mod_from["uses"]
    to_only_from = mod_to["only_from"]
    
    nano_compose.pending_ids[id] = module_name_from
    if can_invoke(module_name_from, module_name_to, from_uses, to_only_from):
        os.write(mod_to["w"], line)
        return
    res_parsed = {"id": id, "error":{
        "codename": "xrpc.forbidden",
        "message": f"module {module_name_from} is not allowed to call {module_name_to}",
    }}
    log("forbidden: ", res_parsed)
    res_line = json.dumps(res_parsed, ensure_ascii=False).encode("utf-8")+b"\n"
    pass_result(nano_compose, module_name_from, res_line, res_parsed)


def pass_result(nano_compose, module_name_from, line, parsed):
    id = parsed["id"]
    module_name_to = nano_compose.pending_ids[id]
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