"""Subscription-backed inference via the user's normally authenticated Codex CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

DEFAULT_MODEL='gpt-6.1-sol'


def cli_environment():
    env=os.environ.copy()
    # Never silently turn a subscription run into separately billed API auth.
    for key in ('OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY','CODEX_THREAD_ID','CODEX_PARENT_THREAD_ID'):
        env.pop(key,None)
    return env


def desktop_app_executable():
    # The Codex desktop app (MSIX) bundles the CLI but does not put it on PATH.
    try:
        import winreg
        root=r'Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,root) as packages:
            names=[]
            for i in range(winreg.QueryInfoKey(packages)[0]):
                name=winreg.EnumKey(packages,i)
                if name.startswith('OpenAI.Codex_'):names.append(name)
            for name in sorted(names,reverse=True):
                with winreg.OpenKey(packages,name) as package:
                    candidate=Path(winreg.QueryValueEx(package,'PackageRootFolder')[0])/'app'/'resources'/'codex.exe'
                if candidate.is_file():return str(candidate)
    except (ImportError,OSError):
        pass
    return None


def executable():
    path=shutil.which('codex.exe') or shutil.which('codex') or desktop_app_executable()
    if not path or Path(path).suffix.lower() in ('.cmd','.bat','.ps1'):
        raise ValueError('Codex CLI executable was not found; install Codex and run codex login')
    return path


def run(args,**kwargs):
    return subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',
                          env=cli_environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),**kwargs)


def status():
    try:
        result=run([executable(),'login','status'],timeout=8)
        ready=result.returncode==0 and 'chatgpt' in (result.stdout+result.stderr).lower()
        return {'configured':ready,'detail':'Signed in with ChatGPT' if ready else 'Run codex login and sign in with ChatGPT to use your subscription'}
    except (ValueError,OSError,subprocess.TimeoutExpired):
        return {'configured':False,'detail':'Codex CLI is unavailable; install it and run codex login'}


def output_schema(symbols=None):
    from .sentiment import schema
    return schema(symbols)


def infer(context,config):
    from .analysis import SYSTEM,PAIR_SYSTEM,validate_output,validate_pair_output
    if not status()['configured']:
        raise ValueError('Codex must be signed in with ChatGPT; run codex login first (API-key login is not used)')
    pairs=context.get('kind')=='pairs'
    schema=output_schema([p['symbol'] for p in context['pairs']] if pairs else None)
    prompt=(PAIR_SYSTEM if pairs else SYSTEM)+'\nUse no tools, files, web search or external knowledge. Return only the required JSON.\nEvidence JSON:\n'+json.dumps(context,ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix='regime-inference-') as folder:
        directory=Path(folder)
        schema_path=directory/'schema.json';output=directory/'summary.json'
        schema_path.write_text(json.dumps(schema),encoding='utf-8')
        args=[executable(),'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check',
              '--sandbox','read-only','--model',config['model'],'--disable','shell_tool',
              '--disable','multi_agent','--disable','enable_mcp_apps','-c','web_search="disabled"',
              '-c','mcp_servers={}','-c','model_reasoning_effort="medium"','--json',
              '--output-schema',str(schema_path),'--output-last-message',str(output),'-']
        try:
            result=run(args,input=prompt,cwd=directory,timeout=120)
        except subprocess.TimeoutExpired:
            raise ValueError('Codex timed out; no new analysis was archived (plan usage may have been consumed)') from None
        except OSError:
            raise ValueError('Codex could not start; no new analysis was archived') from None
        if result.returncode!=0 or not output.is_file():
            raise ValueError('Codex could not complete the analysis; check ChatGPT sign-in, model availability and plan limits. No API fallback or retry was used')
        text=output.read_text(encoding='utf-8')
        usage={}
        for line in result.stdout.splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('type')=='turn.completed':usage=event.get('usage',{})
        parsed=validate_pair_output(text,context['pairs'],context.get('source_ledger',[]),context.get('recent_news')) if pairs else validate_output(text,context.get('source_ledger',[]))
        return parsed,{**usage,'billing_mode':'chatgpt_subscription'},config['model']
