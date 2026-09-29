import sys, tempfile, time, shutil
from pathlib import Path
sys.path.insert(0, ".")
from core.config import GatewayDevice, TestConfig
from core.pipeline import MonitorPipeline
from storage.csv_writer import LogManager
from storage.session import (create_session, open_session, run_history_row,
                             snapshot_gateway_names, collect_run_history)
from core.models import PingRecord

tmp = Path(tempfile.mkdtemp())
DEV = [GatewayDevice(uid="4F9199E139C", name="GW test", eth_ip="192.168.1.101")]

for attempt in (1, 2):
    s = create_session(tmp, gateway_uids=[d.uid.upper() for d in DEV],
                       broker="127.0.0.1:1883", subscribed_topics=["node/#"],
                       tool_version="exp", gateway_names=snapshot_gateway_names(DEV))
    cfg = TestConfig(); cfg.subscribe_gateway_log = True; cfg.packet_merge_window_seconds = 0.3
    logs = LogManager(s.run_dir, rotation=cfg.rotation, flush_interval=0.02,
                      flush_max_records=10, max_queue_size=10000); logs.start()
    p = MonitorPipeline(cfg, DEV, logs, run_id=s.run_id); p._merge_flush_interval = 0.01; p.start()
    for i in range(3):
        p.submit_ping(PingRecord(timestamp="2026-09-25T10:0%d:00.000+07:00" % i,
                                 gateway_uid="4F9199E139C", gateway_name="GW test",
                                 ip_address="192.168.1.101", ping_success=True,
                                 latency_ms=3.0, timeout=False, error=""))
    time.sleep(0.4)
    reports = p.write_reports(s, extra={"reconnects": 0})
    p.stop(); logs.stop()
    s.finish("STOPPED", "exp")
    print("attempt", attempt, "run_id", s.run_id, "reports", len(reports))

rows = collect_run_history(tmp, "", DEV)
print("history rows:", len(rows))
for r in rows:
    print("  ", r["run_id"], "| gw_mon", r["gateways_monitored"],
          "| uids", r["gateway_uids"], "| names", r["gateway_names"],
          "| ping_fail", r["ping_failed"], "| rep", r["reports"])
shutil.rmtree(tmp, ignore_errors=True)
print("OK")
