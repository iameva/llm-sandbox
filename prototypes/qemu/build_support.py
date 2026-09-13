"""Progress and completion checks shared by the builder and its tests."""
import json
import time

EXPECTED_AGENTS = {'claude', 'codex', 'pi', 'omp', 'opencode'}


def read_report(path):
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    if not isinstance(report, dict) or type(report.get('ok')) is not bool:
        raise RuntimeError('invalid build report')
    if report['ok']:
        versions = report.get('versions')
        if (not isinstance(versions, dict) or set(versions) != EXPECTED_AGENTS
                or any(not isinstance(v, str) or not v.strip() for v in versions.values())):
            raise RuntimeError('build report lacks all five tool versions')
    return report


class BuildMonitor:
    def __init__(self, base, share, now=None):
        self.base, self.share = base, share
        self.started = time.monotonic() if now is None else now
        self.report_seen = None
        self.last_print = self.started
        self.stage = None

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        report = read_report(self.share/'build-result.json')
        if report:
            if not report['ok']:
                raise RuntimeError('provisioning failed: '+str(report.get('error', 'unknown error')))
            if self.report_seen is None:
                self.report_seen = now
            if now-self.report_seen > 90:
                raise TimeoutError('installation finished but VM did not shut down within 90 seconds')
        status_path = self.share/'build-status.json'
        status = json.loads(status_path.read_text()) if status_path.exists() else None
        if not status and not report and now-self.started > 600:
            raise TimeoutError('provisioning did not start within ten minutes; inspect console.log')
        if now-self.started > 7200:
            raise TimeoutError('build exceeded two hours')
        console = self.base/'console.log'
        if console.exists() and not report:
            with console.open('rb') as stream:
                stream.seek(max(0, console.stat().st_size-32768))
                if b'Failed to run module scripts_user' in stream.read():
                    raise RuntimeError('cloud-init provisioning failed; inspect provision.log and console.log')
        stage = status.get('stage', 'booting guest') if status else 'booting guest'
        if report:
            stage = 'waiting for guest shutdown'
        if stage != self.stage or now-self.last_print >= 30:
            print(f'Build: {stage} ({int(now-self.started)} seconds elapsed)', flush=True)
            self.stage, self.last_print = stage, now
        return report
