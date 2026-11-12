# Builds tests\media\talk.mp4: about two minutes of real spoken audio (Windows text-to-speech) over a moving
# 1920x1080 test picture. Used by tests\smoke_test.py. Needs ffmpeg.
param([string]$Ffmpeg = "ffmpeg")
$ErrorActionPreference = "Stop"
$dir = Join-Path $PSScriptRoot "media"
New-Item -ItemType Directory -Force $dir | Out-Null
$wav = Join-Path $dir "talk.wav"
$mp4 = Join-Path $dir "talk.mp4"
$text = @"
Why do most people get artificial intelligence completely wrong? Here's why. They think the model is the product, but actually the product is the data.
In 2023, Nvidia sold more chips in one quarter than in the whole of 2019. That is a 400 percent jump, and nobody saw it coming.
Let me tell you the biggest mistake founders make with agents. They automate the wrong job first. You need to start with the boring work, the work nobody wants.
What if your phone could write software while you sleep? That is not science fiction any more. Open source models now run on a laptop with 16 gigabytes of memory.
The truth is that most startups never needed a huge model. A small model, trained on the right data, beats a massive one that knows nothing about your customers.
I was wrong about robots. I thought they were ten years away. Turns out the hard part was never the hands. It was always the software.
Did you know that a single GPU can cost more than a car? And the largest data centres hold 100000 of them. That is the real reason compute is the new oil.
So here is the secret. The future of jobs is not about being replaced. It is about who learns to work with these tools first. Everyone else will be playing catch up.
How fast is this moving? Three years ago, a chatbot could barely finish a sentence. Today it can pass a law exam, write code, and explain a medical scan.
The craziest part is that we are still early. The best products of this decade have not been launched yet. Somebody listening right now is going to build one.
"@
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = 1
$s.SetOutputToWaveFile($wav)
$s.Speak($text)
$s.Dispose()
& $Ffmpeg -y -hide_banner -loglevel error -f lavfi -i "testsrc2=size=1920x1080:rate=30" -i $wav -shortest `
    -c:v libx264 -preset ultrafast -crf 28 -pix_fmt yuv420p -c:a aac -b:a 96k $mp4
if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed" }
Remove-Item $wav
Get-Item $mp4 | Select-Object FullName, Length
