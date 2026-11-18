#!/usr/bin/env python3
"""Host-side lab operator. No Docker socket is ever mounted in a container."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def execute(args, *, capture=False, check=True, timeout=None):
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=capture,
                          check=check, timeout=timeout)


def compose(*args, **kwargs):
    return execute(['docker', 'compose', *args], **kwargs)


def save(name, data):
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def doctor():
    data = {'recorded_at': now(), 'system': platform.platform(), 'kernel': platform.release(),
            'cpu_count': os.cpu_count(), 'disk': shutil.disk_usage(ROOT)._asdict(),
            'collector': 'not_integrated'}
    for label, command in {
        'memory': ['free', '-b'], 'cpu': ['lscpu'],
        'docker': ['docker', 'info', '--format',
                   '{"version":"{{.ServerVersion}}","kernel":"{{.KernelVersion}}","os":"{{.OperatingSystem}}","cpus":{{.NCPU}},"memory":{{.MemTotal}},"cgroup":"{{.CgroupVersion}}","memory_limit":{{.MemoryLimit}},"swap_limit":{{.SwapLimit}}}'],
        'compose': ['docker', 'compose', 'version'],
        'trace_capabilities': ['sh', '-c', 'test -e /sys/kernel/btf/vmlinux && echo BTF=present; test -r /sys/kernel/tracing/available_events && head -n 10 /sys/kernel/tracing/available_events'],
    }.items():
        try:
            result = execute(command, capture=True, check=False)
            data[label] = {'exit_code': result.returncode, 'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()}
        except OSError as error:
            data[label] = {'unavailable': str(error)}
    save('environment.json', data)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if platform.system() != 'Linux':
        print('Run this CLI inside a Linux Docker host (on Windows use WSL).', file=sys.stderr)


def test_services():
    compose('--profile', 'test', '--profile', 'load', '--profile', 'logs', 'up', '-d', '--wait', '--wait-timeout', '180')


def run_tests(suite, extra):
    test_services()
    metadata()
    project = json.loads(compose('config', '--format', 'json', capture=True).stdout)['name']
    path = RESULTS / ('docker-events-' + time.strftime('%Y%m%d-%H%M%S') + '.jsonl')
    with path.open('w', encoding='utf-8') as stream:
        events = subprocess.Popen(['docker', 'events', '--filter',
                                   'label=com.docker.compose.project=' + project,
                                   '--format', '{{json .}}'], stdout=stream, cwd=ROOT)
        try:
            execute([sys.executable, 'tests/run.py', '--suite', suite, *extra])
        finally:
            events.terminate()
            try:
                events.wait(timeout=5)
            except subprocess.TimeoutExpired:
                events.kill()
                events.wait()
            metadata()
            export_results()


def export_results():
    # A named volume is pre-owned by UID 10001, unlike a Linux checkout bind mount.
    # docker cp writes local artifacts as the invoking host user, without chmod 777.
    RESULTS.mkdir(exist_ok=True)
    result = compose('cp', 'load:/results/.', str(RESULTS), capture=True, check=False)
    if result.returncode:
        print('Workload export unavailable (start the load profile first): ' + result.stderr.strip(), file=sys.stderr)
    return result.returncode


def metadata():
    ids = compose('--profile', 'test', '--profile', 'load', '--profile', 'logs', 'ps', '-q', capture=True).stdout.split()
    if not ids:
        return []
    objects = json.loads(execute(['docker', 'inspect', *ids], capture=True).stdout)
    result = []
    for obj in objects:
        pid = obj['State']['Pid']
        context = {}
        for name in ('pid', 'mnt', 'net', 'user', 'cgroup'):
            try:
                context[name] = os.readlink(f'/proc/{pid}/ns/{name}')
            except OSError:
                context[name] = None
        try:
            cgroup = Path(f'/proc/{pid}/cgroup').read_text()
        except OSError:
            cgroup = None
        result.append({'id': obj['Id'], 'name': obj['Name'], 'image': obj['Image'],
                       'service': obj['Config']['Labels'].get('com.docker.compose.service'),
                       'host_pid': pid, 'started_at': obj['State']['StartedAt'],
                       'cgroup': cgroup, 'namespaces': context,
                       'networks': {k: v['IPAddress'] for k, v in obj['NetworkSettings']['Networks'].items()},
                       'limits': {k: obj['HostConfig'].get(k) for k in ('Memory','MemorySwap','NanoCpus','PidsLimit')}})
    snapshot = {'recorded_at': now(), 'containers': result}
    save('container-context.json', snapshot)
    save('container-context-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.json', snapshot)
    return result


def load(args):
    compose('--profile', 'load', 'up', '-d', '--wait', '--wait-timeout', '180')
    RESULTS.mkdir(exist_ok=True)
    stop = threading.Event()
    sample_path = RESULTS / ('resources-' + time.strftime('%Y%m%d-%H%M%S') + '.jsonl')
    ids = [item['id'] for item in metadata()]
    def monitor():
        with sample_path.open('w', encoding='utf-8') as stream:
            while not stop.is_set():
                if ids:
                    result = execute(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids], capture=True, check=False)
                    for line in result.stdout.splitlines():
                        stream.write(json.dumps({'sampled_at': now(), 'source': 'docker_stats', 'sample': json.loads(line)}) + '\n')
                    stream.flush()
                stop.wait(1)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    name = 'llooggeerr-load-' + uuid.uuid4().hex[:12]
    command = ['docker', 'compose', '--profile', 'load', 'run', '--rm', '--no-deps', '--name', name,
               'load', 'python', '-m', 'workloads.run', *args]
    process = subprocess.Popen(command, cwd=ROOT)
    def interrupt(signum, frame):
        execute(['docker', 'stop', '--time', '15', name], check=False, capture=True)
    old = signal.signal(signal.SIGINT, interrupt)
    old_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        result = process.wait()
    finally:
        signal.signal(signal.SIGINT, old)
        signal.signal(signal.SIGTERM, old_term)
        stop.set()
        thread.join(timeout=10)
        export_results()
    print('Resource samples:', sample_path)
    if result:
        raise SystemExit(result)


def report():
    files = sorted(RESULTS.rglob('*.json'))
    truth_audits = []
    for path in sorted(RESULTS.glob('*/truth.jsonl')):
        intents, outcomes = set(), set()
        duplicates = malformed = 0
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                identifier = row.get('operation_id')
                if row.get('record_type') == 'intent':
                    intents.add(identifier)
                elif row.get('record_type') == 'result':
                    duplicates += identifier in outcomes
                    outcomes.add(identifier)
        truth_audits.append({'path': str(path.relative_to(RESULTS)),
                             'intent_operations': len(intents), 'completed_operations': len(outcomes),
                             'unconfirmed_operations': len(intents - outcomes),
                             'results_without_intent': len(outcomes - intents),
                             'duplicate_result_records': duplicates, 'malformed_lines': malformed,
                             'interpretation': 'independent workload operations; not collector events'})
    report = {'generated_at': now(), 'collector_metrics': {
        'status': 'not_integrated', 'eligible_event_truth_count': None,
        'collected_unique_events': None, 'storage_confirmed_events': None,
        'critical_retention': None, 'ordinary_retention': None, 'duplicates': None,
        'unconfirmed': None, 'loss_by_reason': None, 'receive_latency': None,
        'storage_latency': None, 'queue_backlog': None, 'recovery_seconds': None},
        'artifacts': [str(file.relative_to(RESULTS)) for file in files if file.name != 'report.json'],
        'runs': [json.loads(file.read_text(encoding='utf-8')) for file in files if file.name == 'summary.json'],
        'truth_audits': truth_audits,
        'resource_samples': [file.name for file in RESULTS.glob('resources-*.jsonl')]}
    save('report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def oom():
    name = 'llooggeerr-oom-' + uuid.uuid4().hex[:10]
    started = now()
    command = ['--profile', 'fault', 'run', '-d', '--no-deps', '--name', name,
               'oom-probe', 'python', '-c',
               'import time; blocks=[]; [(blocks.append(bytearray(4*1024*1024)),time.sleep(.02)) for _ in range(64)]']
    compose(*command, capture=True)
    try:
        result = execute(['docker', 'wait', name], capture=True, timeout=30)
        obj = json.loads(execute(['docker', 'inspect', name], capture=True).stdout)[0]
        data = {'started_at': started, 'ended_at': now(), 'source': 'docker_runtime',
                'expected': {'OOMKilled': True}, 'actual': obj['State'],
                'limits': {'memory_bytes': obj['HostConfig']['Memory'], 'swap_bytes': obj['HostConfig']['MemorySwap']},
                'passed': obj['State']['OOMKilled'] is True,
                'scope': 'disposable 64MiB cgroup only; no assertion about collector'}
        save('oom-result.json', data)
        print(json.dumps(data, indent=2))
        if not data['passed']:
            raise SystemExit(1)
    finally:
        execute(['docker', 'rm', '-f', name], capture=True, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['doctor','build','up','status','seed','credentials','simulate','load','test','network','recovery','report','context','export','down','reset','oom','sensor-hook'])
    args, extra = parser.parse_known_args()
    if args.command == 'doctor': doctor()
    elif args.command == 'build': compose('build')
    elif args.command == 'up': compose('up', '-d', '--build', '--wait', '--wait-timeout', '180')
    elif args.command == 'status': compose('--profile', 'test', '--profile', 'load', '--profile', 'logs', 'ps', '-a')
    elif args.command == 'seed':
        compose('up', '-d', '--wait', '--wait-timeout', '180')
        print('Seed is idempotently applied by core startup; existing balances are preserved.')
    elif args.command == 'credentials':
        compose('exec', '-T', 'auth', 'python', '-c', "from pathlib import Path; print(Path('/secrets/users.json').read_text())")
    elif args.command in ('simulate','load'):
        load(extra or ['--duration','10','--users','2','--rps','4','--seed','42','--scenario','normal'])
    elif args.command in ('test','network','recovery'):
        suite = {'test':'all','network':'network','recovery':'recovery'}[args.command]
        run_tests(suite, extra)
    elif args.command == 'context':
        metadata()
        print('Wrote results/container-context.json')
    elif args.command == 'report': report()
    elif args.command == 'export':
        raise SystemExit(export_results())
    elif args.command == 'down': compose('--profile','*','down','--timeout','20')
    elif args.command == 'reset':
        if extra != ['--confirm','ERASE-LAB-DATA']:
            parser.error('Reset deletes all lab data. Explicit opt-in: reset --confirm ERASE-LAB-DATA')
        compose('--profile','*','down','--volumes','--timeout','20')
    elif args.command == 'oom': oom()
    elif args.command == 'sensor-hook':
        if len(extra) != 2 or extra[0] not in ('start','stop','config-change'):
            parser.error('sensor-hook {start|stop|config-change} EXTERNAL_RUN_ID')
        RESULTS.mkdir(exist_ok=True)
        with (RESULTS/'sensor-lifecycle.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at':now(),'source':'operator_annotation','action':extra[0], 'external_run_id':extra[1], 'sensor_action_performed_by_this_cli':False})+'\n')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode)
