"""Create missing OTA secret without printing it; preserve existing configuration."""
import os
import secrets
from pathlib import Path
root=Path(__file__).resolve().parent.parent
env=root/'.env'
text=env.read_text(encoding='utf-8') if env.exists() else ''
lines=text.splitlines()
if not any(line.startswith('OTA_INTERNAL_TOKEN=') and line.split('=',1)[1].strip() for line in lines):
    lines=[line for line in lines if not line.startswith('OTA_INTERNAL_TOKEN=')]
    lines.append('OTA_INTERNAL_TOKEN='+secrets.token_urlsafe(48))
    env.write_text('\n'.join(lines)+'\n',encoding='utf-8')
if os.name!='nt':
    os.chmod(env,0o600)
print('Deployment configuration ready; secrets are not displayed.')
