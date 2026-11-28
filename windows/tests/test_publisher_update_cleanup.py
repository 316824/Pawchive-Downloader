"""Exercise the publisher cleanup's OTA inventory against isolated directories."""
import json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ota_update import _process_start_filetime

@unittest.skipUnless(os.name=='nt','Windows PowerShell cleanup inventory')
class PublisherUpdateCleanupTests(unittest.TestCase):
    def test_inventory_keeps_active_unmarked_and_new_update_directories(self):
        with tempfile.TemporaryDirectory(prefix='pawchive_publisher_test_') as temporary:
            root=Path(temporary)
            for name,pid,created in [('old',2147483647,0),('active',os.getpid(),0),('new',2147483647,time.time())]:
                folder=root/('pawchive_update_'+name+'123456');folder.mkdir()
                marker=dict(app='PawchiveDownloader',schema=1,root_name=folder.name,token='a'*32,
                    owner_pid=pid,owner_started=_process_start_filetime(pid),created=created)
                (folder/'owner.json').write_text(json.dumps(marker),encoding='utf-8')
                stamp=created or time.time()-172800;os.utime(folder,(stamp,stamp))
            unmarked=root/'pawchive_update_unmarked123';unmarked.mkdir();os.utime(unmarked,(0,0))
            script=root/'inventory.ps1'
            script.write_text(r'''param([string]$Source,[string]$Fixture)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$tree=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
$functions=$tree.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]},$false)
foreach($function in $functions){. ([scriptblock]::Create($function.Extent.Text))}
$found=@(Get-StaleCacheDirectories -TempDirectory $Fixture -BeforeUtc ([DateTime]::UtcNow.AddHours(-24)))
$result=[pscustomobject]@{names=@($found | ForEach-Object {[IO.Path]::GetFileName($_)});files=@(Get-ChildItem -LiteralPath $Fixture -Directory | ForEach-Object {$_.Name})}
$result | ConvertTo-Json -Compress
''',encoding='utf-8-sig')
            powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
            source=Path(__file__).resolve().parents[1]/'publisher_cleanup.ps1'
            proc=subprocess.run([str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-Source',str(source),'-Fixture',str(root)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            data=json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(data['names'],['pawchive_update_old123456'])
            self.assertEqual(len(data['files']),4)

if __name__=='__main__':unittest.main()
