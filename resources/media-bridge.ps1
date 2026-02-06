# resources/media-bridge.ps1
# HYBRID: Uses embedded C# to bypass PowerShell type issues

$code = @"
using System;
using System.Text;
using System.Threading.Tasks;
using Windows.Media.Control;
using Windows.Storage.Streams;

public class MediaBridge {
    public static async Task GetUpdate() {
        var manager = await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
        var session = manager.GetCurrentSession();
        
        if (session == null) {
            // Check all sessions if current is null
            foreach (var s in manager.GetSessions()) {
                if (s.GetPlaybackInfo().PlaybackStatus == GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing) {
                    session = s;
                    break;
                }
            }
        }

        if (session == null) {
            Console.WriteLine("MEDIA_JSON:{\"title\":\"Not Playing\",\"isPlaying\":false}");
            return;
        }

        var info = await session.TryGetMediaPropertiesAsync();
        var playback = session.GetPlaybackInfo();
        string thumb = null;

        if (info.Thumbnail != null) {
            try {
                var stream = await info.Thumbnail.OpenReadAsync();
                var reader = new DataReader(stream.GetInputStreamAt(0));
                var bytes = new byte[stream.Size];
                await reader.LoadAsync((uint)stream.Size);
                reader.ReadBytes(bytes);
                thumb = Convert.ToBase64String(bytes);
            } catch {}
        }

        // Manual JSON construction to avoid dependencies
        var sb = new StringBuilder();
        sb.Append("MEDIA_JSON:{");
        sb.Append("\"title\":\"" + Escape(info.Title) + "\",");
        sb.Append("\"artist\":\"" + Escape(info.Artist) + "\",");
        sb.Append("\"thumbnail\":\"" + (thumb != null ? "data:image/png;base64," + thumb : "") + "\",");
        sb.Append("\"isPlaying\":" + (playback.PlaybackStatus == GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing ? "true" : "false"));
        sb.Append("}");
        
        Console.WriteLine(sb.ToString());
    }

    static string Escape(string s) {
        if (s == null) return "";
        return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }
}
"@

# Compile the C# code in memory
Add-Type -TypeDefinition $code -Language CSharp -ReferencedAssemblies "Windows.Media.Control", "Windows.Foundation", "Windows.Storage.Streams", "System.Runtime.WindowsRuntime" -ErrorAction SilentlyContinue

# Loop forever calling the C# method
while ($true) {
    try {
        [MediaBridge]::GetUpdate().GetAwaiter().GetResult()
    } catch {
        # Fallback if C# fails
        Write-Output "MEDIA_JSON:{`"title`":`"Not Playing`",`"isPlaying`":false}"
    }
    Start-Sleep -Milliseconds 1000
}