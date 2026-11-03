using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Collections.Generic;
using System.Net;
using System.Web.Script.Serialization;

internal static class EvoCodexServerLauncher
{
    [StructLayout(LayoutKind.Sequential)] struct Basic {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)] struct Io {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)] struct Extended {
        public Basic BasicLimitInformation; public Io IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attr, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, uint size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    static ProcessStartInfo Hidden(string exe, string args) {
        return new ProcessStartInfo(exe,args) { UseShellExecute=false, CreateNoWindow=true,
            RedirectStandardOutput=true, RedirectStandardError=true };
    }
    static void Log(string root,string message) {
        string file=Path.Combine(root,"launcher.log");
        if(File.Exists(file) && new FileInfo(file).Length>32768) File.WriteAllText(file,"");
        File.AppendAllText(file,DateTime.UtcNow.ToString("o")+" "+message+Environment.NewLine);
    }
    static bool ProxyReady() {
        try {
            string file=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),@".opencodex\runtime-port.json");
            var state=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(file));
            int port=Convert.ToInt32(state["port"]), pid=Convert.ToInt32(state["pid"]);
            if(port<1 || port>65535 || Process.GetProcessById(pid).ProcessName!="ocx") return false;
            var request=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:"+port+"/health");
            request.Proxy=null; request.Timeout=2000; request.ReadWriteTimeout=2000;
            using(var response=(HttpWebResponse)request.GetResponse()) return response.StatusCode==HttpStatusCode.OK;
        } catch { return false; }
    }
    static int Main() {
        string root=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),@".config\evo-codex-server");
        string sid=System.Security.Principal.WindowsIdentity.GetCurrent().User.Value;
        using (var mutex=new Mutex(false,@"Local\EvoCodexServer-"+sid)) {
            bool acquired;
            try { acquired=mutex.WaitOne(0); } catch(AbandonedMutexException) { acquired=true; }
            if(!acquired) return 0;
            IntPtr job=IntPtr.Zero;
            try {
                Log(root,"start");
                var config=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(
                    File.ReadAllText(Path.Combine(root,"settings.json")));
                string ocx=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),@"Programs\OpenCodex\ocx.exe");
                // Keep the shared OpenCodex proxy outside this server's Job.
                if(ProxyReady()) Log(root,"proxy-already-ready");
                else if(File.Exists(ocx)) using(var prepare=Process.Start(Hidden(ocx,"ensure"))) {
                    prepare.OutputDataReceived+=(s,e)=>{}; prepare.ErrorDataReceived+=(s,e)=>{};
                    prepare.BeginOutputReadLine(); prepare.BeginErrorReadLine();
                    if(!prepare.WaitForExit(60000)) {
                        prepare.Kill(); Log(root,"prepare-timeout");
                        if(!ProxyReady()) return 1;
                    } else if(prepare.ExitCode!=0) {
                        Log(root,"prepare-exit="+prepare.ExitCode);
                        if(!ProxyReady()) return 1;
                    }
                }
                job=CreateJobObject(IntPtr.Zero,null);
                var limits=new Extended(); limits.BasicLimitInformation.LimitFlags=0x2000;
                int size=Marshal.SizeOf(limits); IntPtr buffer=Marshal.AllocHGlobal(size);
                try {
                    Marshal.StructureToPtr(limits,buffer,false);
                    if(job==IntPtr.Zero || !SetInformationJobObject(job,9,buffer,(uint)size)) { Log(root,"job-create-failed"); return 1; }
                } finally { Marshal.FreeHGlobal(buffer); }
                var start=Hidden(Convert.ToString(config["node"]),"\""+Path.Combine(root,"server.js")+"\"");
                start.WorkingDirectory=root; start.RedirectStandardInput=true;
                using(var process=Process.Start(start)) {
                    process.OutputDataReceived+=(s,e)=>{}; process.ErrorDataReceived+=(s,e)=>{};
                    process.BeginOutputReadLine(); process.BeginErrorReadLine();
                    if(!AssignProcessToJobObject(job,process.Handle)) { process.Kill(); Log(root,"job-assign-failed"); return 1; }
                    process.StandardInput.WriteLine("start"); process.StandardInput.Flush();
                    process.WaitForExit(); Log(root,"node-exit="+process.ExitCode); return process.ExitCode;
                }
            } catch(Exception e) { Log(root,"error="+e.GetType().Name); return 1; }
            finally { if(job!=IntPtr.Zero) CloseHandle(job); mutex.ReleaseMutex(); }
        }
    }
}
