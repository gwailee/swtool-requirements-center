# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Explicitly enabled service-host transport; never executes browser-supplied commands."""
import json
import errno
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time

from server.requirements_driver_ssh import RUNNER, SshDriverTransport


def local_enabled():
    return os.environ.get('SWTOOL_REQUIREMENTS_LOCAL_DRIVER_ENABLED') == '1'


class LocalDriverTransport(SshDriverTransport):
    def __init__(self, config):
        if not local_enabled():
            raise ValueError('管理员尚未启用本机隔离 runner')
        super().__init__(None, '', config, {})

    def _call(self, payload, timeout):
        if payload.get('action') in {'run', 'heartbeat'}:
            payload = dict(payload)
            for key in ('startup_prompt_template', 'completion_prompt_template'):
                template = self.config.get(key)
                if not isinstance(template, str) or not template.strip() or len(template.encode('utf-8')) > 8192:
                    raise ValueError('提示语无效')
                payload[key] = template
        # Validate the fixed executable and every parent before invoking it. The runner
        # additionally validates its root-owned config and enforces bubblewrap isolation.
        executable = Path(RUNNER)
        for path in [executable, *executable.parents]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('runner_isolation_unavailable')
        if not stat.S_ISREG(executable.stat().st_mode) or not os.access(executable, os.X_OK):
            raise RuntimeError('runner_isolation_unavailable')
        request = memoryview(json.dumps(payload, ensure_ascii=False).encode())
        process = subprocess.Popen([RUNNER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        output = bytearray()
        deadline = time.monotonic() + min(timeout, 2100)
        try:
            input_offset = 0
            input_open = True
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    for key, _ in selector.select(min(remaining, 1)):
                        if key.data == 'stdin':
                            try:
                                written = os.write(key.fd, request[input_offset:input_offset + 8192])
                            except BlockingIOError:
                                continue
                            except OSError as exc:
                                if exc.errno == errno.EPIPE:
                                    raise RuntimeError('runner_closed_stdin') from None
                                raise
                            input_offset += written
                            if input_offset == len(request):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                                input_open = False
                        else:
                            try:
                                chunk = os.read(key.fd, min(8192, 65537 - len(output)))
                            except BlockingIOError:
                                continue
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            output.extend(chunk)
                            if len(output) > 65536:
                                raise ValueError('receipt_limit')
                if input_open:
                    process.stdin.close()
            if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                raise RuntimeError('本机受信 runner 拒绝执行；工单保留')
            receipt = json.loads(output)
            if not isinstance(receipt, dict):
                raise RuntimeError('本机 runner 回执格式无效')
            return receipt
        except BaseException:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    process.wait(timeout=5)
            raise RuntimeError('本机 runner 未确认执行结果；保留工单，请停止并人工检查') from None
        finally:
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
