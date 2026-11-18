#!/usr/bin/env python3
"""Run real Compose integration checks and write expected/actual JSON results.

Run from a Linux Docker host: python3 tests/run.py --suite all.
This never deletes volumes, changes host network rules, or accepts arbitrary targets.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import functional

ROOT = Path(__file__).resolve().parents[1]
PRELUDE = (Path(__file__).parent / "probe.py").read_text(encoding="utf-8")
COMPOSE = ["docker", "compose"]
RESULTS: list[dict] = []
RUN_ID = str(uuid.uuid4())


def run(args, *, input=None, timeout=90, check=True):
    result = subprocess.run(args, cwd=ROOT, input=input, text=True, capture_output=True,
                            timeout=timeout)
    if check and result.returncode:
        # stderr never includes credential files; HTTP bodies are not shell arguments.
        raise RuntimeError("command failed: " + " ".join(args[:7]) + "\n" + result.stderr[-1600:])
    return result


def compose(*args, **kwargs):
    return run(COMPOSE + list(args), **kwargs)


def record(name, expected, actual, passed=None, **extra):
    item = {"run_id": RUN_ID, "operation_id": str(uuid.uuid4()),
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "independent_test_harness", "name": name, "expected": expected, "actual": actual,
            "passed": expected == actual if passed is None else bool(passed), **extra}
    RESULTS.append(item)
    print(("PASS " if item["passed"] else "FAIL ") + name, flush=True)
    return item


def probe(service, code, *, timeout=120):
    result = compose("exec", "-T", service, "python", "-", input=PRELUDE + "\n" + code,
                     timeout=timeout)
    lines = [line[len("LAB_RESULT="):] for line in result.stdout.splitlines()
             if line.startswith("LAB_RESULT=")]
    if not lines:
        raise RuntimeError("probe returned no structured result for " + service)
    value = json.loads(lines[-1])
    for item in value["records"]:
        record(**item, source_container=service)
    return value.get("extra")


def inspect(service):
    identifier = compose("ps", "-a", "-q", service).stdout.strip()
    if not identifier:
        raise RuntimeError("service container missing: " + service + "; start the test/logs/load profiles")
    return json.loads(run(["docker", "inspect", identifier]).stdout)[0]


def wait_healthy(service, timeout=60):
    deadline = time.monotonic() + timeout
    state = {}
    while time.monotonic() < deadline:
        state = inspect(service)["State"]
        if state.get("Health", {}).get("Status") == "healthy":
            return True
        time.sleep(0.5)
    return state


def functional_suite():
    for service in ["web", "customer", "auth", "core", "employee", "db", "events", "receiver"]:
        actual = inspect(service)["State"].get("Health", {}).get("Status")
        record("healthy container " + service, "healthy", actual)
    probe("customer-client", functional.CUSTOMER)
    probe("employee-client", functional.EMPLOYEE)
    probe("manager", functional.EVENTS)


def connection(source, target, port, allowed):
    target_info = inspect(target)
    source_networks = set(inspect(source)["NetworkSettings"]["Networks"])
    networks = target_info["NetworkSettings"]["Networks"]
    ips = [(name, data["IPAddress"]) for name, data in networks.items()
           if data.get("IPAddress") and (not allowed or name in source_networks)]
    if not ips:
        record("TCP " + source + " -> " + target, "target IP available", [], False)
        return
    for network, address in ips:
        code = "actual=tcp(" + repr(address) + "," + str(port) + ")\nfinish(actual)\n"
        actual = probe(source, code)
        record("TCP " + source + " -> " + target + " via " + network,
               allowed, actual, destination_ip=address, destination_port=port)


def network_suite():
    bindings = inspect("web")["NetworkSettings"].get("Ports", {}).get("8080/tcp") or []
    record("web port is actually published", True, bool(bindings))
    for binding in bindings:
        record("runtime web binding is loopback", "127.0.0.1", binding.get("HostIp"))
        port = int(binding.get("HostPort", "0"))
        record("runtime web host port is assigned", True, 1 <= port <= 65535)
        if binding.get("HostIp") != "127.0.0.1" or not 1 <= port <= 65535:
            continue
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path in ("/health", "/"):
            try:
                with opener.open("http://127.0.0.1:" + str(port) + path, timeout=5) as response:
                    status, content = response.status, response.read().decode("utf-8")
                record("host loopback HTTP " + path, 200, status, source_container="docker_host")
                if path == "/health":
                    record("host reaches web health", "web", json.loads(content).get("service"))
                else:
                    record("host receives customer web page", True, "llooggeerr" in content)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                record("host loopback HTTP " + path, 200, type(exc).__name__ + ": " + str(exc), False,
                       source_container="docker_host")
    allowed = [("customer-client","web",8080),("web","customer",8080),
               ("customer","auth",8080),("customer","core",8080),
               ("employee-client","employee",8080),("employee","auth",8080),
               ("employee","core",8080),("core","auth",8080),("core","db",5432),
               ("manager","core",8080),("manager","events",8080),
               ("manager","receiver",8080),("log-forwarder","receiver",8080)]
    denied = [("customer-client","db",5432),("customer-client","core",8080),
              ("customer-client","manager",8080),("customer-client","receiver",8080),
              ("web","db",5432),("employee-client","db",5432),
              ("employee-client","receiver",8080),("customer","manager",8080),
              ("employee","manager",8080),("web","manager",8080),
              ("log-forwarder","db",5432)]
    for args in allowed:
        connection(*args, allowed=True)
    for args in denied:
        connection(*args, allowed=False)
    for source,target in [("customer-client","web"),("employee-client","employee"),
                          ("manager","core"),("log-forwarder","receiver")]:
        probe(source, 'check("allowed HTTP '+source+' -> '+target+'",200,request("http://'+target+':8080","/health")[0]);finish()')
    probe("customer", 'check("core requires service credential",403,request("http://core:8080","/accounts")[0]);finish()')
    cfg = json.loads(compose("--profile", "test", "--profile", "load", "--profile", "logs", "config", "--format", "json").stdout)
    entry_services = sorted(name for name, service in cfg["services"].items()
                            if "web-entry" in service.get("networks", {}))
    record("only web joins host entry bridge", ["web"], entry_services)
    for name, network in cfg.get("networks", {}).items():
        record("network isolation mode " + name, name != "web-entry", network.get("internal", False))
    for name, service in cfg["services"].items():
        record("explicit networks or network isolation " + name, True,
               bool(service.get("networks")) or service.get("network_mode") == "none")
        record("no privileged container " + name, False, service.get("privileged", False))
        for port in service.get("ports", []):
            record("published port is loopback " + name, "127.0.0.1", port.get("host_ip"))
            record("only web is published", "web", name)


SNAPSHOT = '''
base="http://web:8080"
alice,bob=login(base,"alice"),login(base,"bob")
finish([balance(base,alice,"A001"),balance(base,bob,"A002")])
'''


def business_recovery():
    for service in ["web", "customer", "core", "db"]:
        before = probe("customer-client", SNAPSHOT)
        record("before failure banking works " + service, True, len(before) == 2)
        started = time.monotonic()
        try:
            compose("stop", "-t", "5", service)
            failure = probe("customer-client", '''
base="http://web:8080"
if SERVICE in ("core","db"):
    token=login(base,"alice")
    code=request(base,"/accounts",token=token,timeout=5)[0]
else:
    code=request(base,"/accounts",timeout=5)[0]
finish(code)
'''.replace("SERVICE", repr(service)))
            record("failure is observable " + service, [0,500,502,503,504], failure,
                   failure in (0,500,502,503,504), fault_class="business_service")
            status = probe("log-forwarder", 'finish(request("http://receiver:8080","/health")[0])')
            record("receiver unaffected by banking failure " + service, 200, status)
        finally:
            compose("up", "-d", service)
            record("service recovered " + service, True, wait_healthy(service))
        after = probe("customer-client", SNAPSHOT)
        record("balances preserved across restart " + service, before, after,
               recovery_seconds=round(time.monotonic()-started,3))
    request_id = "recovery-" + uuid.uuid4().hex
    transfer_code = '''
base="http://web:8080"
token=login(base,"alice")
status,value,_=request(base,"/transfers","POST",{"request_id":REQUEST_ID,"from_account":"A001","to_account":"A002","amount_cents":3},token)
finish({"status":status,"transaction_status":value.get("status")})
'''.replace("REQUEST_ID",repr(request_id))
    record("persistent idempotence initial request", {"status":200,"transaction_status":"completed"}, probe("customer-client",transfer_code))
    before = probe("customer-client",SNAPSHOT)
    compose("restart","db")
    record("DB healthy after idempotence restart",True,wait_healthy("db"))
    record("persistent idempotence replay", {"status":200,"transaction_status":"completed"}, probe("customer-client",transfer_code))
    record("restart does not duplicate transfer",before,probe("customer-client",SNAPSHOT))


def receiver_control(**values):
    return probe("manager", 'finish(request("http://receiver:8080","/control","POST",'+repr(values)+',internal=True)[0])')


def receiver_recovery():
    body = {"server_id":"test-server","event_id":"test-"+uuid.uuid4().hex,"payload":{"source":"independent-test","message":"synthetic"}}
    code = 'status,value,elapsed=request("http://receiver:8080","/events","POST",'+repr(body)+',internal=True);finish({"status":status,"stored":value.get("stored"),"duplicate":value.get("duplicate"),"elapsed":elapsed})'
    initial = probe("log-forwarder",code)
    record("receiver new message committed",[201,True,False],[initial["status"],initial["stored"],initial["duplicate"]])
    duplicate = probe("log-forwarder",code)
    record("receiver identical resend deduplicated",[200,True,True],[duplicate["status"],duplicate["stored"],duplicate["duplicate"]])
    conflict = dict(body,payload={"message":"different payload"})
    status = probe("log-forwarder", 'finish(request("http://receiver:8080","/events","POST",'+repr(conflict)+',internal=True)[0])')
    record("receiver event ID collision rejected",409,status)
    compose("restart","receiver")
    record("receiver healthy after restart",True,wait_healthy("receiver"))
    duplicate = probe("log-forwarder",code)
    record("receiver dedup survives restart",[200,True,True],[duplicate["status"],duplicate["stored"],duplicate["duplicate"]])
    try:
        record("configure mock availability failure",200,receiver_control(disconnect=True))
        failed = probe("log-forwarder",code)
        record("mock receiver availability failure observable",503,failed["status"],fault_class="log_transport_mock")
        record("banking unaffected by receiver failure",True,len(probe("customer-client",SNAPSHOT))==2)
        receiver_control(delay_seconds=0.25)
        delayed = probe("log-forwarder",code)
        record("receiver response delay measured",">= 0.25 seconds",delayed["elapsed"],delayed["elapsed"]>=0.25)
        receiver_control(write_failure=True)
        failed_body = dict(body,event_id="write-failure-"+uuid.uuid4().hex)
        failed_code = 'status,value,_=request("http://receiver:8080","/events","POST",'+repr(failed_body)+',internal=True);finish({"status":status,"received":value.get("received"),"stored":value.get("stored")})'
        failed = probe("log-forwarder",failed_code)
        record("receipt is distinct from failed storage",{"status":507,"received":True,"stored":False},failed,fault_class="storage_mock")
        receiver_control()
        retry = probe("log-forwarder",failed_code)
        record("retry commits after storage recovery",{"status":201,"received":True,"stored":True},retry)
    finally:
        receiver_control()
    info = inspect("receiver")
    candidates = [(n,v) for n,v in info["NetworkSettings"]["Networks"].items() if n in inspect("log-forwarder")["NetworkSettings"]["Networks"]]
    if len(candidates)!=1:
        raise RuntimeError("receiver/log-forwarder must share exactly one dedicated network")
    network,_ = candidates[0]
    try:
        run(["docker","network","disconnect",network,info["Id"]])
        disconnected = probe("log-forwarder",code)
        record("actual log network disconnect fails",0,disconnected["status"],fault_class="log_network")
        record("management path stays available",200,probe("manager",'finish(request("http://receiver:8080","/health")[0])'))
        record("banking continues during log network outage",True,len(probe("customer-client",SNAPSHOT))==2)
    finally:
        run(["docker","network","connect","--alias","receiver",network,info["Id"]])
    restored = probe("log-forwarder",code)
    record("actual log network restored with dedup",[200,True],[restored["status"],restored["duplicate"]])
    try:
        compose("kill","-s","SIGKILL","events")
        state = inspect("events")["State"]
        record("test event worker forced exit",137,state.get("ExitCode"),fault_class="limited_test_service")
        record("banking continues after event worker kill",True,len(probe("customer-client",SNAPSHOT))==2)
    finally:
        compose("up","-d","events")
        record("event worker recovered",True,wait_healthy("events"))


def recovery_suite():
    business_recovery()
    receiver_recovery()


def workload_suite():
    probe("load", r'''
import subprocess
import sys
import signal

def execute_run(interrupt=False):
    args=[sys.executable,"-m","workloads.run","--duration","30" if interrupt else "3",
          "--users","3","--rps","5","--failure-ratio","0.35","--seed","71",
          "--scenario","normal","--output","/results"]
    existing=set(Path("/results").glob("*/truth.jsonl"))
    child=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        if interrupt:
            deadline=time.monotonic()+20
            observed=False
            while time.monotonic()<deadline and child.poll() is None:
                for truth_path in set(Path("/results").glob("*/truth.jsonl"))-existing:
                    for line in truth_path.read_text().splitlines():
                        try:
                            row=json.loads(line)
                        except ValueError:
                            continue
                        observed=observed or (row.get("record_type")=="result" and row.get("phase")=="workload")
                if observed:
                    break
                time.sleep(0.1)
            if not observed:
                raise RuntimeError("no completed workload request before interrupt deadline")
            child.send_signal(signal.SIGTERM)
        stdout,stderr=child.communicate(timeout=35)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    if child.returncode:
        raise RuntimeError("workload exited "+str(child.returncode)+": "+stderr[-800:])
    descriptor=json.loads(stdout.strip().splitlines()[-1])
    summary_path=Path(descriptor["summary"])
    summary=json.loads(summary_path.read_text())
    truth=[json.loads(line) for line in (summary_path.parent/"truth.jsonl").read_text().splitlines()]
    return summary,truth

def verify(summary,truth,label):
    results=[row for row in truth if row.get("record_type")=="result"]
    intents=[row for row in truth if row.get("record_type")=="intent"]
    work=[row for row in results if row.get("phase")=="workload"]
    check(label+" complete intent/result pairs",sorted(row["operation_id"] for row in intents),sorted(row["operation_id"] for row in results))
    check(label+" unique operation results",len(results),len({row["operation_id"] for row in results}))
    check(label+" request count reconciles",len(work),summary["total_requests"])
    check(label+" successes reconcile",sum(bool(row["success"]) for row in work),summary["success_count"])
    check(label+" failures reconcile",sum(not row["success"] for row in work),summary["failure_count"])
    check(label+" expected failures distinguished",0,summary["unexpected_outcome_count"])
    check(label+" truth source independent",True,all(row.get("source")=="independent_workload" for row in intents+results))
    check(label+" collector remains unmeasured","not_integrated",summary["collector"]["status"])
    check(label+" collector retention not fabricated",None,summary["collector"]["critical_retention_rate"])
    check(label+" measured latency percentiles",True,all(summary["latency_ms"][key] is not None for key in ["mean","p50","p95","p99"]))
    check(label+" target and actual fields separate",True,all(key in summary for key in ["target_rps","actual_rps","target_request_count","total_requests"]))
    return work

first,truth1=execute_run()
work1=verify(first,truth1,"first run")
check("mixed load records successes and failures",True,first["success_count"]>0 and first["failure_count"]>0)
second,truth2=execute_run()
work2=verify(second,truth2,"repeated run")
def plan_rows(rows):
    return {row["operation_id"].split("-")[-1]:[row["operation_type"],row["requested_service"],row["target"],row["scenario_label"],row["user_id"]] for row in rows}
a,b=plan_rows(work1),plan_rows(work2)
overlap=sorted(set(a)&set(b))
check("same seed reproduces overlapping operation slots",True,bool(overlap) and all(a[key]==b[key] for key in overlap))
stopped,truth3=execute_run(interrupt=True)
verify(stopped,truth3,"interrupted run")
check("SIGTERM safely stops generator","signal_received",stopped["stop_reason"])
check("SIGTERM stops before requested duration",True,stopped["actual_duration_seconds"]<stopped["requested_duration_seconds"])
finish({"runs":[first["run_id"],second["run_id"],stopped["run_id"]]})
''', timeout=120)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite",choices=["functional","network","recovery","load","all"],default="all")
    parser.add_argument("--output",default="results/test-results.json")
    args=parser.parse_args()
    start=dt.datetime.now(dt.timezone.utc).isoformat()
    suites={"functional":functional_suite,"network":network_suite,"recovery":recovery_suite,"load":workload_suite}
    try:
        for name, fn in suites.items():
            if args.suite in (name,"all"):
                try:
                    fn()
                except Exception as exc:
                    record(name+" suite execution", "completed",type(exc).__name__+": "+str(exc),False)
    except KeyboardInterrupt:
        record("test run interrupted","completed","interrupted",False)
    output=ROOT/args.output
    output.parent.mkdir(parents=True,exist_ok=True)
    summary={"schema_version":1,"run_id":RUN_ID,"suite":args.suite,"started_at":start,
             "finished_at":dt.datetime.now(dt.timezone.utc).isoformat(),
             "passed":sum(r["passed"] for r in RESULTS),"failed":sum(not r["passed"] for r in RESULTS),
             "collector_status":"not_integrated","checks":RESULTS}
    output.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"passed":summary["passed"],"failed":summary["failed"],"output":str(output)},ensure_ascii=False))
    return 1 if summary["failed"] else 0


if __name__=="__main__":
    raise SystemExit(main())
