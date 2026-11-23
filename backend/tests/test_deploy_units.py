# -*- coding: utf-8 -*-
"""定时任务单元 + 安装脚本的回归锁（纯标准库，不依赖 systemd）。

锁住的都是「改了就出事」的点：

1. 抓数时间必须是 `Mon 04:30`：历史文档曾把 `systemctl list-timers` 的 NEXT（04:30 +
   随机延迟）误写成 04:44，进而可能被「顺手改成 04:44」。
2. 每日对齐必须是 `*-*-* 05:20` 且不随机延迟（发布要可预测），并且排在周一抓数窗口之后。
3. 两个 service **都不得**加 `NoNewPrivileges` / `PrivateTmp`：refresh.sh 与 release.sh
   都要 `sudo -n systemctl restart shikar`，加了直接废掉（与 shikar.service 的加固策略刻意不同）。
4. release.service 必须自带两道护栏：先 fetch（拉不到就不发，避免「报成功但没对齐」）、
   抓数在跑就跳过（避免 rsync 替换运行中的脚本 + 双重重启）。
5. 安装脚本不许碰不可再生数据（`data/`）、不许 `rm -rf`。
6. 文档必须进 docs/README.md 索引（生产副本不携带 docs/，此时跳过）。
"""
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))  # backend/tests -> 仓库根
UNIT_DIR = os.path.join(REPO, "deploy", "systemd")
INSTALLER = os.path.join(REPO, "deploy", "install-timers.sh")
DOC_NAME = "48-定时任务运维手册（抓数每周+版本对齐每日）.md"

UNITS = (
    "shikar-refresh.timer",
    "shikar-refresh.service",
    "shikar-release.timer",
    "shikar-release.service",
)


def parse_unit(name):
    """把 unit 文件解析成 {section: {key: value}}；同名键保留第一个。"""
    path = os.path.join(UNIT_DIR, name)
    assert os.path.isfile(path), "缺少单元文件：%s" % path
    sections, cur = {}, None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                cur = sections.setdefault(m.group(1), {})
                continue
            if cur is None or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cur.setdefault(key.strip(), value.strip())
    return sections


def raw_text(name):
    with open(os.path.join(UNIT_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def code_lines(path):
    """只返回可执行行（去掉空行与注释）——注释里写「永不 rm -rf」不该被当成违规。"""
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            yield line


def test_four_units_parse():
    for name in UNITS:
        sections = parse_unit(name)
        assert "Unit" in sections, "%s 缺 [Unit]" % name
        assert "Service" in sections or "Timer" in sections, "%s 缺 [Service]/[Timer]" % name


def test_refresh_timer_pins_weekly_schedule():
    timer = parse_unit("shikar-refresh.timer")["Timer"]
    assert timer["OnCalendar"] == "Mon 04:30", "抓数必须是周一 04:30（不是 04:44，那是 list-timers 的 NEXT）"
    assert timer["Persistent"] == "true", "错过一次要补跑（Persistent=true）"
    assert timer["RandomizedDelaySec"] == "900", "随机延迟是线上现状（0–15 分钟），改它等于改真实触发时间"
    install = parse_unit("shikar-refresh.timer").get("Install", {})
    assert install.get("WantedBy") == "timers.target"


def test_release_timer_pins_daily_schedule():
    timer = parse_unit("shikar-release.timer")["Timer"]
    assert timer["OnCalendar"] == "*-*-* 05:20", "版本对齐必须每天 05:20"
    assert timer["Persistent"] == "true"
    assert "RandomizedDelaySec" not in timer, "发布要可预测，不能加随机延迟"
    install = parse_unit("shikar-release.timer").get("Install", {})
    assert install.get("WantedBy") == "timers.target"


def test_refresh_service_exec_and_timeout():
    svc = parse_unit("shikar-refresh.service")["Service"]
    assert svc["Type"] == "oneshot"
    assert svc["User"] == "ubuntu"
    assert svc["WorkingDirectory"] == "/opt/shikar"
    assert svc["ExecStart"] == "/opt/shikar/deploy/refresh.sh"
    # 实测单次 11–12 分钟；超时防的是「卡死 → 下周被静默跳过」，不是防慢
    assert int(svc["TimeoutStartSec"]) >= 3600


def test_release_service_guards():
    svc = parse_unit("shikar-release.service")["Service"]
    assert svc["Type"] == "oneshot"
    assert svc["User"] == "ubuntu"
    assert svc["ExecStart"] == "/opt/shikar/deploy/release.sh --yes", "无人值守必须 --yes，否则卡在交互确认"
    assert "fetch" in svc["ExecStartPre"], "必须先强制 fetch：拉不到远端就不发，避免报成功但没对齐"
    assert "shikar-refresh" in svc["ExecCondition"], "抓数在跑时必须跳过当天对齐"
    assert int(svc["TimeoutStartSec"]) >= 600


def test_services_do_not_block_sudo():
    # 注释里可以解释「为什么不能加」，但指令区里绝对不能出现
    for name in ("shikar-refresh.service", "shikar-release.service"):
        svc = parse_unit(name)["Service"]
        assert "NoNewPrivileges" not in svc, "%s 加了 NoNewPrivileges 会让 sudo -n 失效" % name
        assert "PrivateTmp" not in svc, "%s 的脚本要写部署目录外的真实路径，别加 PrivateTmp" % name


def test_installer_is_safe():
    assert os.path.isfile(INSTALLER), "缺少安装脚本：%s" % INSTALLER
    with open(INSTALLER, "r", encoding="utf-8") as fh:
        body = fh.read()
    for name in UNITS:
        assert name in body, "安装脚本里没有 %s" % name
    code = list(code_lines(INSTALLER))
    assert "set -uo pipefail" in code
    joined = "\n".join(code)
    assert "rm -rf" not in joined, "安装脚本不许 rm -rf"
    # 删除只允许针对单元目录里的文件（不动 data/）
    for line in code:
        if "rm -f" in line:
            assert "$UNIT_DIR" in line, "安装脚本删了 $UNIT_DIR 之外的东西：%s" % line
    # 安装器只装/卸单元，不许代跑任务（手动采集是不可逆动作，要人点头）
    assert "systemctl start" not in joined, "安装脚本不许 systemctl start（尤其 shikar-refresh）"
    assert "enable --now" in joined
    if os.name == "posix":  # Windows 没有可执行位，跳过
        assert os.stat(INSTALLER).st_mode & 0o111, "安装脚本必须可执行（git update-index --chmod=+x）"


def test_docs_indexed():
    # 生产副本只同步 backend/web/scripts/deploy（deploy/release.sh 的 CODE_DIRS），
    # docs/ 不会上服务器；release.sh 会在服务器跑 pytest，这里必须能安全跳过，
    # 否则每次发布都会因「服务器上没有这份文档」而失败并自动回滚。
    doc = os.path.join(REPO, "docs", DOC_NAME)
    if not os.path.isfile(doc):
        pytest.skip("docs/ 未随代码同步到本副本（生产只同步 backend/web/scripts/deploy）")
    with open(os.path.join(REPO, "docs", "README.md"), "r", encoding="utf-8") as fh:
        index = fh.read()
    assert DOC_NAME in index, "新增文档必须进 docs/README.md 索引"
