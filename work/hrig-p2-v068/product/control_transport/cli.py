"""Submit a prepared, pinned local transport request to public extension.run."""
import argparse, json, subprocess, sys
from pathlib import Path
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True);parser.add_argument('--jobs-dir',required=True)
    args=parser.parse_args();request=Path(args.request).resolve();document=json.loads(request.read_bytes())
    expected=Path(__file__).with_name('job_worker.py').resolve()
    assert document['command']=='extension.run'
    assert Path(document['params']['manifest']['script']['file']).resolve()==expected
    public=Path(__file__).resolve().parents[4]/'tools/blenderctl/cli.py'
    process=subprocess.run([sys.executable,'-B',str(public),'--jobs-dir',str(Path(args.jobs_dir).resolve()),'--timeout','900','request',str(request)])
    return process.returncode
if __name__=='__main__':raise SystemExit(main())
