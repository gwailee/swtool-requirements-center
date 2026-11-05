# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 上海商江网络科技有限公司版权所有
# 软工云（swtool.com）开发团队；uqtool@163.com
"""Pinned SSH transport to the SSH user's requirements runner."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import tempfile
import threading


RUNNER = ".config/swtool/bin/swtool-requirements-driver"
REMOTE_COMMAND = "python3 -c " + shlex.quote("""import os,pwd,stat
from pathlib import Path
uid=os.geteuid(); home=Path(pwd.getpwuid(uid).pw_dir)
runner=home/'.config/swtool/bin/swtool-requirements-driver'
legacy=Path('/usr/local/libexec/swtool-requirements-driver')
def user_chain_safe():
    current=Path('/')
    for part in home.parts[1:]:
        current=current/part
        st=current.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=(uid if current==home else 0) or st.st_mode&0o022 or not stat.S_ISDIR(st.st_mode): return False
    for part in ('.config','swtool','bin'):
        current=current/part
        try: st=current.lstat()
        except FileNotFoundError: return True
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=uid or st.st_mode&0o022 or not stat.S_ISDIR(st.st_mode): return False
    return True
try:
    user_chain_valid=user_chain_safe()
    if user_chain_valid:
        try: runner.lstat()
        except FileNotFoundError: runner=legacy
except OSError: user_chain_valid=False
def safe(path):
    current=Path('/')
    for part in path.parts[1:]:
        current=current/part
        st=current.lstat(); inside=current==home or current.is_relative_to(home)
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=(uid if inside else 0) or st.st_mode&0o022 or (current!=path and not stat.S_ISDIR(st.st_mode)): return False
    st=path.lstat()
    return stat.S_ISREG(st.st_mode) and st.st_uid==(0 if path==legacy else uid) and st.st_nlink==1 and not st.st_mode&(0o022 if path==legacy else 0o077) and os.access(path,os.X_OK)
try: valid=user_chain_valid and safe(runner)
except OSError: valid=False
if not valid:
    code='runner_missing' if not runner.exists() else 'runner_config_invalid'
    print('{"ok":false,"error":"'+code+'"}'); raise SystemExit(71)
os.execv(str(runner),[str(runner)])""")
ERRORS = {
    "ssh_unavailable": ("ssh_unavailable", "SSH 目标不可连接或认证失败，请检查 SSH 地址、端口和凭据"),
    "runner_missing": ("runner_missing", "目标终端未安装需求驱动 runner，请在该终端右上角使用一键部署"),
    "runner_config_missing": ("runner_config_missing", "SSH 用户尚未完成需求驱动运行配置，请对该 SSH 重新一键部署"),
    "runner_disabled": ("runner_disabled", "需求驱动运行配置尚未启用；请对该 SSH 重新一键部署，或检查保留的自定义配置"),
    "runner_config_invalid": ("runner_config_invalid", "需求驱动 runner 配置无效，请管理员检查配置"),
    "cli_unavailable": ("cli_unavailable", "目标终端的 CLI 命令、协议或依赖不可用；SSH 用户模式使用该用户已有登录，请检查该用户能否运行 CLI"),
    "runner_isolation_unavailable": ("runner_isolation_unavailable", "目标终端未满足受信隔离条件，需求驱动已停止"),
    "runner_policy_rejected": ("runner_policy_rejected", "目标终端策略拒绝本次需求驱动，请检查管理员策略"),
    "receipt_invalid": ("receipt_invalid", "远端回执无效；工单状态未确认，请检查远端状态后再操作"),
    "completion_evidence_missing": ("completion_evidence_missing", "CLI 已运行，但无法独立确认干净 Git 工作区、提交及远端 push；工单未关闭，请检查工作目录和验收命令"),
    "verification_command_required": ("verification_command_required", "请 SSH 所有者在 Runner 配置 verification_command 为真实项目验收命令 argv；git diff --check 不能作为功能验收，工单未关闭"),
    "trusted_project_execution_required": ("trusted_project_execution_required", "请 SSH 所有者在 Runner 配置显式设置 trusted_project_execution=true，确认允许以该用户执行项目验收代码；系统不会自动授权"),
    "ticket_changed": ("ticket_changed", "工单标题或正文已修改，或旧运行缺少内容绑定；旧候选已保留且不会关闭新版工单，请核对后显式选择新执行基线"),
    "verification_policy_changed": ("verification_policy_changed", "SSH 所有者的验收或部署策略已修改，旧候选已保留但不自动复用；请由所有者核对候选与新策略，并显式选择新基线重新启动"),
    "workspace_dirty": ("workspace_dirty", "源项目目录存在用户未提交的修改；请先自行提交或保存这些修改，再启动需求驱动，系统不会自动提交或回撤用户文件"),
    "repository_or_remote_drift": ("repository_or_remote_drift", "源目录缺少明确的 Git 基线/远端，或基线、分支、远端及候选链发生变化；请选择具有明确远端且与远端一致的干净 Git 项目后重试"),
    "push_unconfirmed": ("push_unconfirmed", "Git push 尚未确认，或源项目/远端已发生变化；请核对远端提交和项目目录，系统不会覆盖漂移或回撤用户文件"),
    "unsafe_candidate_path": ("unsafe_candidate_path", "候选文件包含不允许的路径、符号链接、硬链接或过大文件，或未产生可提交修改；请检查独立项目目录与候选文件"),
    "verification_tree_changed": ("verification_tree_changed", "项目验收改变了候选文件；请将验收产物写入独立输出目录后重试，工单未关闭"),
    "deployment_configuration_required": ("deployment_configuration_required", "工单要求部署，请 SSH 所有者配置固定 deployment_command、deployment_health_command，并显式开启 deploy_authorized；工单未关闭"),
    "deployment_unconfirmed": ("deployment_unconfirmed", "部署或健康检查未确认，系统不会盲目重复部署；请 SSH 所有者核查部署状态后人工恢复，工单未关闭"),
    "verification_failed": ("verification_failed", "CLI 已运行，但配置的验收命令失败；工单未关闭"),
    "invalid_working_directory": ("invalid_working_directory", "工作目录必须是 SSH 用户可读写的绝对目录"),
    "cli_workspace_required": ("cli_workspace_required", "请选择 SSH 用户已配置工作区内的独立项目目录，不能使用 HOME、配置目录或包含控制面文件的目录；请勿使用符号链接目录"),
    "rollback_not_owned": ("rollback_not_owned", "共享 SSH 工作目录无法证明提交由本次工单独占，请人工检查并回撤；自动回撤未执行"),
    "cli_protocol_invalid": ("cli_protocol_invalid", "CLI 输出不符合约定协议，请检查 CLI 版本与运行日志"),
    "cli_protocol_eof": ("cli_protocol_eof", "CLI 在完成工单前关闭了输出，请检查 CLI 登录、启动参数与运行日志"),
    "cli_protocol_response_invalid": ("cli_protocol_response_invalid", "CLI 返回了无效协议响应，请检查 CLI 版本与运行日志"),
    "cli_protocol_terminated": ("cli_protocol_terminated", "CLI 协议进程已终止，工单未关闭，请检查运行日志"),
    "cli_result_failed": ("cli_result_failed", "CLI 报告本轮执行失败，工单未关闭，请检查运行日志"),
    'cli_api_start_failed': ('cli_api_start_failed', 'SWTool CLI API 启动失败，请重新一键部署并检查 CLI 版本与配置'),
    'cli_api_auth_failed': ('cli_api_auth_failed', 'SWTool CLI API 身份验证失败，已停止发送工单'),
    'cli_api_request_failed': ('cli_api_request_failed', 'SWTool CLI API 拒绝请求，请检查 CLI 版本与运行配置'),
    'cli_api_response_limit': ('cli_api_response_limit', 'SWTool CLI API 响应超出限制，工单未关闭'),
    'cli_api_submission_unconfirmed': ('cli_api_submission_unconfirmed', '无法确认工单提示是否已提交，已禁止自动重发，请人工检查会话'),
    'cli_api_interaction_unconfirmed': ('cli_api_interaction_unconfirmed', 'CLI 非交互审批响应未确认，已停止且禁止自动重发，请检查会话'),
    'cli_api_interaction_limit': ('cli_api_interaction_limit', 'CLI 非交互审批已达本工单运行上限，已停止，工单未关闭'),
    'cli_api_disconnected': ('cli_api_disconnected', 'SWTool CLI API 连接连续失败，已停止；会话记录已保留'),
    'cli_api_process_exited': ('cli_api_process_exited', 'SWTool CLI API 子进程已退出，工单未关闭；会话记录已保留'),
    'cli_model_auth_failed': ('cli_model_auth_failed', 'SWTool CLI 模型登录失效，请配置或登录模型后重试'),
    'cli_model_configuration_failed': ('cli_model_configuration_failed', 'SWTool CLI 未产生模型响应，请检查模型、登录与服务配置'),
    'cli_api_version_required': ('cli_api_version_required', '请通过一键部署升级 SWTool CLI 到 3.6.0 或更新版本后使用接口驱动'),
    'cli_process_containment_unavailable': ('cli_process_containment_unavailable', '目标 Linux 缺少安全回收 CLI 子任务的能力，当前无法启动持续执行，请管理员检查内核与 Python pidfd 支持'),
    "cli_approval_required": ("cli_approval_required", "CLI 需要人工批准，请在目标终端处理后手动恢复"),
    "selected_cli_session_active": ("selected_cli_session_active", "所选 SSH 已有对应 CLI 的 tmux 会话，但尚不能安全复用，请在目标终端检查后恢复"),
    "selected_cli_session_probe_unavailable": ("selected_cli_session_probe_unavailable", "无法安全确认所选 SSH 的现有 tmux 中是否运行 CLI，请检查该会话后重试"),
    "selected_cli_session_ambiguous": ("selected_cli_session_ambiguous", "所选 SSH 中检测到多个对应 CLI，请人工选择目标会话或关闭多余 CLI 后恢复；工单尚未下发"),
    "selected_cli_session_not_ready": ("selected_cli_session_not_ready", "无法确认 CLI 当前输入状态，请在目标终端处理后恢复"),
    "selected_cli_session_changed": ("selected_cli_session_changed", "绑定的 tmux 窗格或 CLI 进程已变化，需求驱动已安全暂停，请确认目标会话后恢复"),
    "selected_cli_session_busy": ("selected_cli_session_busy", "CLI 正忙或正在等待审批，请在目标终端处理后恢复"),
    "selected_cli_session_send_unconfirmed": ("selected_cli_session_send_unconfirmed", "无法确认提示词是否已发送，已停止自动重发，请在目标终端确认后恢复"),
    "selected_cli_session_workspace_mismatch": ("selected_cli_session_workspace_mismatch", "现有 CLI 工作目录与需求配置不一致，请在目标终端切换至配置目录后恢复；工单未下发"),
    "cli_checkpoint_limit": ("cli_checkpoint_limit", "本工单继续提示次数已达上限，已暂停；请检查目标终端后处理"),
    "cli_phase_timeout": ("cli_phase_timeout", "本工单 CLI 运行时限已到，已暂停追加提示；请检查目标终端状态"),
    "selected_cli_session_interrupted": ("selected_cli_session_interrupted", "已停止追加提示词，但用户 CLI 可能仍在运行，请在目标终端确认后恢复"),
    "termination_unconfirmed": ("termination_unconfirmed", "尚未确认 CLI 与工具任务全部停止；请重试停止。服务已退出仍无法确认时，请人工检查目标机后恢复"),
    "runner_rejected_check_admin_log": ("runner_rejected_check_admin_log", "需求驱动 Runner 拒绝或执行失败，请检查目标终端运行日志"),
}


def safe_completion_summary(text, config=None):
    """Display-only bounded summary; never used as publication evidence."""
    if not isinstance(text, str):
        return ''
    text = text[:4000]
    secrets = set()
    def collect(values):
        for key, value in values.items():
            if isinstance(value, dict):
                collect(value)
            elif re.search(r'(?i)password|passwd|secret|token|api[_-]?key|credential', str(key)) and isinstance(value, str) and len(value) >= 4:
                secrets.add(value)
    collect(dict(os.environ)); collect(config or {})
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'-----BEGIN [^\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^\n]*PRIVATE KEY-----|$)', '[已隐藏]', text)
    text = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', text)
    text = ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127))
    text = re.sub(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[已隐藏]@', text)
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;]+', r'\1[已隐藏]', text)
    text = re.sub(r"(?i)((?:[\w-]*(?:password|passwd|secret|token|api[_-]?key|credential)[\w-]*)[\"']?\s*[:=]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)", r'\1[已隐藏]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,})\b', '[已隐藏]', text)
    text = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[联系方式已隐藏]', text)
    text = re.sub(r'(?<![A-Za-z0-9])(?:\+?\d[ ()-]*){7,15}(?![A-Za-z0-9])', '[联系方式已隐藏]', text)
    return text.strip()[:4000]


def _safe_detail(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r'[\x00-\x1f\x7f\r\n]+', ' ', value)
    value = re.sub(r'(?i)(token|api[_ -]?key|password|passwd|secret|authorization)(\s*[:=]\s*)[^,;\s&]+', r'\1\2[redacted]', value)
    return re.sub(r'\s+', ' ', value).strip()[:240] or None


def _safe_runner_api_detail(value):
    if not isinstance(value, str):
        return None
    methods = "GET|POST|PATCH|PUT|DELETE"
    endpoints = (
        r"/global/health|/session|/session/status|/session/message|"
        r"/session/\{session_id\}|/session/\{session_id\}/message|"
        r"/session/\{session_id\}/message/\{message_id\}|"
        r"/session/\{session_id\}/prompt_async|/permission|/question|/unknown"
    )
    exceptions = (
        "URLError|TimeoutError|ConnectionError|ConnectionResetError|"
        "ConnectionRefusedError|BrokenPipeError|OSError|ChildProcessExited"
    )
    pattern = rf"method=(?:{methods}) endpoint=(?:{endpoints}) exception=(?:{exceptions})"
    return value if re.fullmatch(pattern, value, flags=re.ASCII) else None


def ticket_content_sha256(ticket):
    return hashlib.sha256(json.dumps({key: ticket.get(key, '') for key in ('id', 'title', 'body')},
                                     ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class RunnerDiagnostic(RuntimeError):
    def __init__(self, code, saved_ssh_tmux_probe=None, detail=None):
        self.code = code if code in ERRORS else "receipt_invalid"
        self.saved_ssh_tmux_probe = saved_ssh_tmux_probe
        if self.code in {"cli_api_disconnected", "cli_api_process_exited"}:
            detail = _safe_runner_api_detail(detail)
        self.detail = _safe_detail(detail)
        super().__init__(ERRORS[self.code][1])


class RunnerUnavailable(RunnerDiagnostic):
    pass


def _diagnostic(code, saved_ssh_tmux_probe=None, detail=None):
    if not isinstance(code, str):
        return RunnerDiagnostic("receipt_invalid")
    if code in {"runner_missing", "runner_config_missing", "ssh_unavailable"}:
        return RunnerUnavailable(code)
    if code in ERRORS:
        if code in {"cli_api_disconnected", "cli_api_process_exited"}:
            detail = _safe_runner_api_detail(detail)
        elif code not in {
            "cli_result_failed", "cli_model_auth_failed", "cli_model_configuration_failed",
            "cli_protocol_response_invalid", "cli_approval_required"}:
            detail = None
        return RunnerDiagnostic(code, saved_ssh_tmux_probe, detail)
    return RunnerDiagnostic("receipt_invalid")


def validate_config(config):
    """Validate the client-facing subset. Isolation and command policy are remote-owned."""
    if config.get("tool") != "swtool":
        raise ValueError("需求驱动仅支持 SWTOOL；旧 Codex/Claude 配置不会自动迁移，请重新选择 SWTOOL 并保存")
    generation = config.get("run_generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation):
        raise ValueError("runner_generation_missing")
    session = config.get("run_session_id")
    if not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{32}", session):
        raise ValueError("runner_session_missing")
    for key in ("startup_prompt_template", "completion_prompt_template"):
        template = config.get(key)
        if not isinstance(template, str) or not template.strip() or len(template.encode("utf-8")) > 8192:
            raise ValueError("提示语必须非空且不超过 8 KiB")


class SshDriverTransport:
    def __init__(self, saved, password, config, host_key, private_key=None):
        validate_config(config)
        self.saved, self.password = saved, password
        self.private_key = private_key
        self.config, self.host_key = dict(config), dict(host_key)
        self._runs = {}
        self._lock = threading.Lock()

    def _call(self, payload, timeout):
        address = self.host_key["address"]
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("禁止连接内网目标")
        with tempfile.TemporaryDirectory(prefix="swtool-driver-") as directory:
            hosts = Path(directory) / "known_hosts"
            hosts.write_text("swtool-driver " + self.host_key["algorithm"] + " " + self.host_key["publicKey"] + "\n")
            hosts.chmod(0o600)
            command = ["ssh", "-T", "-F", "/dev/null", "-oConnectTimeout=10", "-oStrictHostKeyChecking=yes",
                       "-oClearAllForwardings=yes", "-oForwardAgent=no", "-oForwardX11=no",
                       "-oPermitLocalCommand=no", "-oProxyCommand=none", "-oProxyJump=none",
                       "-oHostKeyAlias=swtool-driver", "-oUserKnownHostsFile=" + str(hosts),
                       "-oGlobalKnownHostsFile=/dev/null", "-oHostName=" + address,
                       "-p", str(self.saved.target.port)]
            env = os.environ.copy()
            if self.private_key:
                key_file = Path(directory) / 'identity'
                key_file.write_text(self.private_key)
                key_file.chmod(0o600)
                command += ['-oBatchMode=yes', '-oIdentitiesOnly=yes', '-i', str(key_file)]
            elif self.password:
                helper = Path(directory) / "askpass"
                helper.write_text('#!/bin/sh\nprintf "%s\\n" "$SWTOOL_SSH_PASSWORD"\n')
                helper.chmod(0o700)
                env.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE="force", DISPLAY=":0",
                           SWTOOL_SSH_PASSWORD=self.password)
                command += ["-oBatchMode=no", "-oPreferredAuthentications=password",
                            "-oPubkeyAuthentication=no", "-oNumberOfPasswordPrompts=1"]
            else:
                command += ["-oBatchMode=yes"]
            command += [self.saved.target.username + "@" + self.saved.target.host, REMOTE_COMMAND]
            try:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, env=env)
            except OSError:
                raise RunnerUnavailable("ssh_unavailable") from None
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False).encode())
            except BrokenPipeError:
                # A fixed-path probe can reject before consuming the request.
                pass
            finally:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            output = bytearray()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = __import__('time').monotonic() + timeout
            try:
                while selector.get_map():
                    remaining = deadline - __import__('time').monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    for key, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(key.fd, min(8192, 65537 - len(output)))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > 65536:
                            raise ValueError('receipt_limit')
                returncode = process.wait(timeout=max(0.1, deadline - __import__('time').monotonic()))
            except (TimeoutError, ValueError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
                if len(output) > 65536:
                    raise RunnerDiagnostic("receipt_invalid") from None
                raise RuntimeError("SSH 回执超时；远端状态未知，已保留工单且禁止自动重试") from None
            if returncode in (71, 72):
                raise RunnerUnavailable("runner_missing" if returncode == 71 else "runner_config_missing")
            if returncode == 255:
                raise RunnerUnavailable("ssh_unavailable")
            try:
                receipt = json.loads(output)
            except (ValueError, UnicodeDecodeError):
                raise RunnerDiagnostic("receipt_invalid") from None
            if not isinstance(receipt, dict):
                raise RunnerDiagnostic("receipt_invalid")
            if receipt.get("ok") is False:
                raise _diagnostic(receipt.get("error"), receipt.get('saved_ssh_tmux_probe'), receipt.get('error_detail'))
            if returncode:
                raise RuntimeError("受信远端 runner 拒绝或执行失败；工单未关闭")
            return receipt

    @staticmethod
    def _run_id(requirement_id, generation, ticket_id=None):
        identity = requirement_id + ":" + generation + ":" + str(ticket_id or "")
        return hashlib.sha256(identity.encode()).hexdigest()[:32]

    def _identity(self, session_id, generation=None):
        owner = str(self.config.get("owner_id") or "")
        target = str(self.config.get("target_fingerprint") or self.host_key.get("fingerprint") or "")
        if not target:
            public_key = self.host_key.get("publicKey")
            if not public_key:
                raise ValueError("target_identity_missing")
            target = "SHA256:" + hashlib.sha256(public_key.encode()).hexdigest()
        identity = {"session_id": session_id, "generation": generation or self.config["run_generation"],
                    "target_fingerprint": target, "owner_id": owner}
        # Derive the gateway name from the authenticated saved record, never UI config.
        saved_owner = getattr(self.saved, 'owner_id', None)
        saved_session = getattr(self.saved, 'session_id', None)
        if saved_owner is not None or saved_session is not None:
            if (not isinstance(saved_owner, str) or saved_owner != owner or
                    not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}', saved_owner) or
                    not isinstance(saved_session, str) or not saved_session or len(saved_session) > 256):
                raise ValueError('saved_ssh_identity_invalid')
            digest = hashlib.sha256(f'{saved_owner}:saved:{saved_session}'.encode()).hexdigest()[:24]
            identity['saved_ssh_tmux'] = 'sw-' + digest
        return identity

    def status(self, requirement_id, session_id, ticket_id=None):
        payload = {"protocol": 1, "action": "status", "run_id": self._run_id(requirement_id, self.config["run_generation"], ticket_id),
                   **self._identity(session_id)}
        receipt = self._call(payload, 30)
        if receipt.get("ok") is not True:
            raise RuntimeError("远端状态查询失败；禁止恢复或新启动")
        return receipt

    def completion_recovery_available(self, requirement_id):
        """Read-only confirmation used before cancelling an already cleaned-up worker."""
        current = self._runs.get(requirement_id)
        if not current or self.config.get('tool') != 'swtool':
            return False
        report = self.status(requirement_id, current['session_id'], current['ticket']['id'])
        return report.get('status') == 'completion_recoverable' and report.get('resumable') is True

    def logs(self, requirement_id, session_id, ticket_id, cursor=0, run_id=None):
        current_run = self._run_id(requirement_id, self.config["run_generation"], ticket_id)
        payload = {"protocol": 1, "action": "logs", "run_id": current_run,
                   "log_scope": hashlib.sha256(requirement_id.encode()).hexdigest(),
                   "client_run_id": run_id, "cursor": cursor,
                   **self._identity(session_id)}
        receipt = self._call(payload, 20)
        if receipt.get("ok") is not True or receipt.get("run_id") != current_run:
            raise RuntimeError("远端日志查询失败")
        if not isinstance(receipt.get("text"), str) or type(receipt.get("cursor")) is not int:
            raise RunnerDiagnostic("receipt_invalid")
        return receipt

    def run(self, policy, requirement_id, ticket, cancel):
        generation = self.config["run_generation"]
        run_id = self._run_id(requirement_id, generation, ticket.id)
        session_id = self.config["run_session_id"]
        identity = self._identity(session_id)
        payload = {"protocol": 1, "action": "run", "run_id": run_id,
                   "log_scope": hashlib.sha256(requirement_id.encode()).hexdigest(),
                   **identity,
                   "tool": self.config["tool"],
                   "startup_prompt_template": self.config["startup_prompt_template"],
                   "completion_prompt_template": self.config["completion_prompt_template"],
                   "push_authorized": self.config.get("push_authorized") is True,
                   "required_deployment": self.config.get("required_deployment") is True,
                   "requested": {key: self.config.get(key) for key in ('workspace', 'branch', 'remote')},
                   "ticket": {"id": ticket.id, "title": ticket.title, "body": ticket.body}}
        with self._lock:
            current = self._runs.get(requirement_id)
            if current and current.get("run_id") != run_id:
                raise RuntimeError("远端工单已有活动执行")
            self._runs[requirement_id] = payload
        heartbeat_stop = threading.Event()
        def heartbeat():
            failures = 0
            while not heartbeat_stop.wait(5):
                if cancel.is_set():
                    return
                try:
                    self._call({"protocol": 1, "action": "heartbeat", "run_id": run_id, **identity}, 12)
                    failures = 0
                except Exception:
                    failures += 1
                    # Preserve the same tmux identity through transient disconnects,
                    # but stop renewing after a bounded number of failed attempts.
                    if failures >= 6 or heartbeat_stop.wait(min(30, 2 ** failures)):
                        return
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            receipt = self.status(requirement_id, session_id, ticket.id)
            def verify_content(report):
                if (self.config['tool'] == 'swtool' and report.get('status') != 'unknown'
                        and report.get('ticket_content_sha256') != ticket_content_sha256(payload['ticket'])):
                    raise _diagnostic('ticket_changed')
            verify_content(receipt)
            if receipt.get('status') == 'unknown':
                if self.config.get("current_ticket_started") is True:
                    raise RuntimeError("已启动工单的远端会话记录丢失；禁止自动重新启动")
                receipt = self._call(payload, 30)
            elif (self.config['tool'] == 'swtool' and receipt.get('status') == 'completion_recoverable'
                  and receipt.get('resumable') is True):
                # run() is reached only by the controller's explicit resume/bounded retry;
                # status polling alone never starts another publication attempt.
                receipt = self._call(dict(payload, _resume_completion=True), 30)
            elif (self.config['tool'] == 'swtool' and receipt.get('status') == 'completed'
                  and (receipt.get('receipt') or {}).get('retryable_error') == 'cli_turn_incomplete'
                  and (receipt.get('receipt') or {}).get('terminated') is True
                  and (receipt.get('receipt') or {}).get('cli_exit') == 0
                  and (receipt.get('receipt') or {}).get('same_session') is True):
                receipt = self._call(dict(payload, _continue_cli_turn=True), 30)
            elif receipt.get('status') not in ('active', 'completed'):
                raise RuntimeError("已有远端会话已停止；请先检查回执或手动恢复")
            limit = self.config.get("max_runtime_seconds")
            deadline = (__import__('time').monotonic() + float(limit)
                        if isinstance(limit, (int, float)) and limit > 0 else None)
            if receipt.get('status') == 'unknown':
                raise RuntimeError("远端会话不存在；禁止冒充恢复或新启动")
            while receipt.get('status') == 'active' and (deadline is None or __import__('time').monotonic() < deadline):
                if cancel.is_set():
                    raise RuntimeError("收到停止请求；等待远端取消确认")
                __import__('time').sleep(2)
                failures = 0
                while True:
                    try:
                        receipt = self.status(requirement_id, session_id, ticket.id)
                        break
                    except Exception:
                        failures += 1
                        if failures >= 6:
                            raise RuntimeError("远端状态连续查询失败；保留会话身份，请稍后恢复") from None
                        if cancel.wait(min(30, 2 ** failures)):
                            raise RuntimeError("收到停止请求；等待远端取消确认") from None
            verify_content(receipt)
            if receipt.get('receipt'):
                receipt = receipt['receipt']
            if receipt.get('ok') is False and 'error' in receipt:
                raise _diagnostic(receipt['error'], detail=receipt.get('error_detail'))
            from types import SimpleNamespace
            if cancel.is_set():
                raise RuntimeError("收到停止请求；等待远端取消确认")
            if (receipt.get('ok') is False and receipt.get('retryable_error') == 'cli_start_failed'
                    and receipt.get('side_effects') is False and receipt.get('terminated') is True
                    and isinstance(receipt.get('cli_exit'), int) and receipt['cli_exit'] != 0):
                with self._lock:
                    self._runs.pop(requirement_id, None)
                return SimpleNamespace(exit_code=receipt['cli_exit'], summary='isolated_cli_failed_before_import',
                                       changed_paths=(), retryable_error='cli_start_failed', verified=False, pushed=False)
            if (self.config['tool'] == 'swtool' and receipt.get('ok') is False
                    and receipt.get('retryable_error') == 'cli_turn_incomplete'
                    and receipt.get('terminated') is True and receipt.get('same_session') is True
                    and receipt.get('cli_exit') == 0):
                return SimpleNamespace(exit_code=0, summary='CLI 本轮尚未完成工单', changed_paths=(),
                                       retryable_error='cli_turn_incomplete', verified=False, pushed=False)
            if self.config.get('tool') == 'swtool' and receipt.get('execution_mode') == 'ssh-user':
                summary = receipt.get('completion_summary')
                if (receipt.get('ok') is not True or receipt.get('cli_exit') != 0
                        or not isinstance(summary, str) or len(summary) > 4000
                        or receipt.get('isolation') != 'selected-workspace'):
                    raise RuntimeError("runner 未提供有效的 SSH-user CLI 完成回执")
                with self._lock:
                    self._runs.pop(requirement_id, None)
                return SimpleNamespace(exit_code=0,
                                       summary="CLI 工单完成回执已核验",
                                       changed_paths=(), retryable_error=None,
                                       verified=True, pushed=False, execution_mode='ssh-user')
            required = {"ok", "verified", "cli_exit", "verify_exit", "pushed", "commit", "base", "changed_paths"}
            if not required.issubset(receipt) or receipt.get("ok") is not True or receipt.get("verified") is not True or receipt.get("cli_exit") != 0 or receipt.get("verify_exit") != 0:
                raise RuntimeError("runner 未提供有效的 CLI 与验收回执")
            if not re.fullmatch(r"[a-f0-9]{40,64}", str(receipt["commit"])) or not re.fullmatch(r"[a-f0-9]{40,64}", str(receipt["base"])):
                raise RuntimeError("runner SHA 回执无效")
            if receipt["pushed"] is not True:
                raise RuntimeError("远端未确认 Git push")
            if ((payload.get("required_deployment") is True or receipt.get("required_deployment") is True)
                    and (receipt.get("deployed") is not True or receipt.get("health_verified") is not True)):
                raise RuntimeError("工单要求部署，但 Runner 未提供部署与健康检查回执；工单未关闭")
            with self._lock:
                self._runs.pop(requirement_id, None)
            return SimpleNamespace(exit_code=0, summary=((safe_completion_summary(receipt.get("completion_summary"), self.config) + "\n")
                                             if receipt.get("completion_summary") else "") +
                                           "verified_push:" + receipt["commit"] + " base:" + receipt["base"],
                                   changed_paths=tuple(receipt["changed_paths"]), retryable_error=None,
                                   verified=True, pushed=True)
        except Exception:
            # Retain the remote identity until an explicit cancel confirms no process remains.
            raise
        finally:
            heartbeat_stop.set()

    def cancel(self, requirement_id):
        generation = self.config["run_generation"]
        with self._lock:
            current = self._runs.get(requirement_id)
        session_id = self.config["run_session_id"]
        ticket_id = self.config.get("current_ticket_id")
        if current:
            run_id = current["run_id"]
        elif ticket_id:
            run_id = self._run_id(requirement_id, generation, ticket_id)
        else:
            raise RuntimeError("没有可取消的持久工单身份")
        payload = {"protocol": 1, "action": "cancel", "run_id": run_id,
                   **self._identity(session_id)}
        receipt = self._call(payload, 30)
        if receipt.get("cancelled") is not True or receipt.get("active") is not False:
            raise RuntimeError("无法确认远端进程已停止；保留执行租约并禁止重试")
        with self._lock:
            self._runs.pop(requirement_id, None)

    def preflight(self, requirement_id):
        """Ask the trusted endpoint to prove its configured isolation and CLI policy."""
        receipt = self._call({"protocol": 1, "action": "preflight",
                              **self._identity(self.config['run_session_id']),
                              "tool": self.config["tool"], "push_authorized": self.config.get("push_authorized") is True,
                   "required_deployment": self.config.get("required_deployment") is True,
                              "requested": {key: self.config.get(key) for key in ('workspace', 'branch', 'remote')},
                              "run_id": self._run_id(requirement_id, self.config["run_generation"])}, 30)
        mode = receipt.get('execution_mode', 'isolated')
        if (receipt.get("ok") is not True or
                (mode, receipt.get('isolation')) not in
                {('isolated', 'bubblewrap-unshare-all'), ('ssh-user', 'ssh-user')} or
                receipt.get('protocol') != 1 or receipt.get('tmux') is not True):
            raise RunnerDiagnostic("runner_isolation_unavailable")
        if receipt.get("tool") != self.config.get("tool"):
            raise RunnerDiagnostic("runner_policy_rejected")
        return receipt

    def rollback(self, requirement_id, commit, expected_head):
        """Owner-confirmed, compare-and-swap revert; never resets or force-pushes."""
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit) or not re.fullmatch(r"[a-f0-9]{40,64}", expected_head):
            raise ValueError("rollback SHA 无效")
        ticket_id = self.config.get("last_ticket_id") or self.config.get("current_ticket_id")
        generation = self.config.get("last_run_generation") or self.config.get("run_generation")
        session_id = self.config.get("last_run_session_id") or self.config.get("run_session_id")
        if not ticket_id or not generation or not session_id:
            raise RuntimeError("没有可回撤的持久工单身份")
        run_id = self._run_id(requirement_id, generation, ticket_id)
        receipt = self._call({"protocol": 1, "action": "revert", "run_id": run_id,
                              **self._identity(session_id, generation),
                              "commit": commit, "expected_head": expected_head}, 300)
        if receipt.get("ok") is not True or receipt.get("reverted") != commit or receipt.get("pushed") is not True:
            raise RuntimeError("远端未确认回撤；请检查 Git 工作区")
        return receipt
