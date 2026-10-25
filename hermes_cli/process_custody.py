"""OS-only Linux descendant custody, shared with the updater's contained commands.

The native caller remains the authority. These helpers only retain and reap the
kernel children of one execution; target output is never a control channel.
"""

LINUX_REAPER_SOURCE = r'''
def arm_subreaper():
    if not sys.platform.startswith('linux'):
        return False
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False

def owned_children():
    found = []
    me = os.getpid()
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % entry, 'rb') as stream:
                stat = stream.read()
            if int(stat[stat.rindex(b')') + 2:].split()[1]) == me:
                found.append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return found
'''


NATIVE_CUSTODIAN_SOURCE = LINUX_REAPER_SOURCE + r'''
import fcntl, json, os, selectors, signal, socket, struct, subprocess, sys, termios, time

control = socket.socket(fileno=int(sys.argv[1]))
os.set_inheritable(control.fileno(), False)
config = json.loads(sys.argv[2])
selector = selectors.DefaultSelector()
selector.register(control, selectors.EVENT_READ, 'control')
wake_read, wake_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
selector.register(wake_read, selectors.EVENT_READ, 'signal')
signal.set_wakeup_fd(wake_write)
requested = []
for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(signum, lambda number, frame: requested.append(number))
signal.signal(signal.SIGCHLD, lambda number, frame: None)

def send(kind, **fields):
    try:
        control.send(json.dumps({'kind': kind, **fields}).encode())
    except OSError:
        pass

def birth(pid):
    try:
        with open('/proc/%s/stat' % pid, 'rb') as stream:
            data = stream.read()
        return int(data[data.rindex(b')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None

def close_output():
    for fd in (1, 2):
        try:
            os.close(fd)
        except OSError:
            pass

def resize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

def spawn_target():
    gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
    master = None
    if config['pty']:
        import pty
        pid, master = pty.fork()
    else:
        pid = os.fork()
    if pid == 0:
        control.close()
        os.close(gate_write)
        allowed = os.read(gate_read, 1)
        os.close(gate_read)
        if allowed != b'g':
            os._exit(125)
        if not config['pty']:
            os.setsid()
        os.execvpe(config['argv'][0], config['argv'], os.environ)
    os.close(gate_read)
    target_birth = birth(pid)  # the unreaped child cannot exec/exit/reuse this identity yet
    if master is not None:
        resize(master, 30, 120)
    send('started', pid=pid, birth=target_birth)
    if target_birth is not None:
        os.write(gate_write, b'g')
    os.close(gate_write)
    if master is not None:
        os.set_blocking(master, False)
        selector.register(master, selectors.EVENT_READ, 'pty')
        selector.register(0, selectors.EVENT_READ, 'input')
    return pid, master

if not arm_subreaper():
    send('refused', error='Linux child custody could not be established; target was not started.')
    sys.exit(125)
send('ready', pid=os.getpid(), birth=birth(os.getpid()))
started = False
main_pid = None
main_code = None
main_sent = False
master = None
stop_at = None
term_at = None
signalled = set()
lost = False
while True:
    if requested:
        requested.clear()
        stop_at = time.monotonic() + 10
        signalled.clear()
    if started:
        empty = False
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                empty = True
                break
            if not pid:
                break
            signalled.discard(pid)
            if pid == main_pid:
                main_code = os.waitstatus_to_exitcode(status)
        if main_code is not None and not main_sent:
            main_sent = True
            send('main_exit', code=main_code, remaining=not empty)
            if master is None:
                close_output()
        if empty:
            send('scope_empty', code=main_code)
            break
    elif stop_at is not None or lost:
        send('scope_empty', code=None)
        break
    if stop_at is not None and term_at is None:
        term_at = time.monotonic()
        if main_pid in owned_children():
            try:
                os.kill(main_pid, signal.SIGTERM)  # windows-footgun: ok — verified unreaped child
            except OSError as exc:
                send('unknown', error='Owned parent termination was refused: %s' % exc)
    if stop_at is not None and time.monotonic() >= term_at + config['grace']:
        for pid in owned_children():
            if pid in signalled:
                continue
            signalled.add(pid)
            try:
                os.kill(pid, signal.SIGKILL)  # windows-footgun: ok — Linux-only owned unreaped child
            except OSError as exc:
                send('unknown', error='Owned child termination was refused: %s' % exc)
        if time.monotonic() >= stop_at:
            send('unknown', error='Owned child exit could not be confirmed.')
            stop_at = None
    for key, _ in selector.select(0.2 if stop_at is not None else None):
        if key.data == 'signal':
            try:
                os.read(wake_read, 4096)
            except OSError:
                pass
        elif key.data == 'control':
            packet = control.recv(65536)
            if not packet:
                selector.unregister(control)
                lost = True
                if not config['persist'] or not started:
                    stop_at = time.monotonic() + 10
                    signalled.clear()
                continue
            message = json.loads(packet)
            if message.get('op') == 'go' and not started and stop_at is None:
                main_pid, master = spawn_target()
                started = True
            elif message.get('op') == 'stop':
                term_at = None
                stop_at = time.monotonic() + 10
                signalled.clear()
            elif message.get('op') == 'resize' and master is not None:
                resize(master, message['rows'], message['cols'])
        elif key.data == 'input':
            data = os.read(0, 4096)
            if data and master is not None:
                try:
                    os.write(master, data)
                except OSError:
                    selector.unregister(0)
            else:
                selector.unregister(0)
        elif key.data == 'pty':
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b''
            if data:
                os.write(1, data)
            else:
                selector.unregister(master)
                try:
                    selector.unregister(0)
                except KeyError:
                    pass
                os.close(master)
                master = None
                close_output()
control.close()
sys.exit(0)
'''
