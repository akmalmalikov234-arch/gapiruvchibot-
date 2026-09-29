import os
import uuid
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from edge_tts import Communicate

BASE_DIR = Path(__file__).resolve().parent
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Natural Voice Mini App")

app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")


class VoiceRequest(BaseModel):
    text: str
    voice: str = "en-US-AriaNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse("""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Natural Voice</title>

<style>
*{box-sizing:border-box}

body{
    margin:0;
    min-height:100vh;
    font-family:Inter,Arial,sans-serif;
    background:
        radial-gradient(circle at top,#26356d 0,#10152d 38%,#070a14 100%);
    color:#fff;
    display:flex;
    justify-content:center;
    align-items:center;
    padding:20px;
}

.app{
    width:min(760px,100%);
    background:rgba(16,20,40,.86);
    border:1px solid rgba(255,255,255,.1);
    box-shadow:0 25px 80px rgba(0,0,0,.45);
    backdrop-filter:blur(20px);
    border-radius:28px;
    padding:28px;
}

h1{
    margin:0 0 8px;
    font-size:32px;
}

.sub{
    color:#aeb7d8;
    margin-bottom:25px;
}

textarea{
    width:100%;
    min-height:220px;
    resize:vertical;
    border:1px solid #30395f;
    border-radius:18px;
    background:#0a0e1c;
    color:#fff;
    padding:18px;
    font-size:17px;
    line-height:1.6;
    outline:none;
}

textarea:focus{
    border-color:#7184ff;
}

.controls{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:14px;
    margin-top:15px;
}

label{
    display:block;
    color:#aeb7d8;
    font-size:13px;
    margin-bottom:7px;
}

select,input{
    width:100%;
    background:#0a0e1c;
    color:#fff;
    border:1px solid #30395f;
    border-radius:12px;
    padding:13px;
    outline:none;
}

button{
    width:100%;
    margin-top:18px;
    border:0;
    border-radius:15px;
    padding:16px;
    color:white;
    font-size:16px;
    font-weight:700;
    cursor:pointer;
    background:linear-gradient(135deg,#6575ff,#9b5cff);
    transition:.2s;
}

button:hover{
    transform:translateY(-1px);
    filter:brightness(1.1);
}

button:disabled{
    opacity:.55;
    cursor:not-allowed;
}

.status{
    min-height:24px;
    margin-top:15px;
    color:#aeb7d8;
}

audio{
    width:100%;
    margin-top:12px;
}

.download{
    display:none;
    text-align:center;
    color:#9ba9ff;
    margin-top:12px;
    text-decoration:none;
}

@media(max-width:600px){
    .app{padding:20px;border-radius:20px}
    h1{font-size:26px}
    .controls{grid-template-columns:1fr}
}
</style>
</head>

<body>

<div class="app">

<h1>Natural Voice</h1>
<div class="sub">
Convert your text into natural-sounding speech.
</div>

<textarea id="text"
placeholder="Write something here..."></textarea>

<div class="controls">

<div>
<label>Voice</label>
<select id="voice">
<option value="en-US-AriaNeural">Aria — Female</option>
<option value="en-US-GuyNeural">Guy — Male</option>
<option value="en-US-JennyNeural">Jenny — Female</option>
<option value="en-US-DavisNeural">Davis — Male</option>
<option value="en-GB-SoniaNeural">Sonia — British Female</option>
<option value="en-GB-RyanNeural">Ryan — British Male</option>
</select>
</div>

<div>
<label>Speed</label>
<select id="rate">
<option value="-20%">Slow</option>
<option value="-10%">Slightly slow</option>
<option value="+0%">Normal</option>
<option value="+10%">Fast</option>
<option value="+20%">Very fast</option>
</select>
</div>

</div>

<button id="generate">Generate Voice</button>

<div class="status" id="status"></div>

<audio id="player" controls></audio>

<a
id="download"
class="download"
download="voice.mp3">
Download audio
</a>

</div>

<script>

const text = document.getElementById("text");
const voice = document.getElementById("voice");
const rate = document.getElementById("rate");
const generate = document.getElementById("generate");
const status = document.getElementById("status");
const player = document.getElementById("player");
const download = document.getElementById("download");

generate.onclick = async () => {

    const value = text.value.trim();

    if (!value) {
        status.textContent = "Write some text first.";
        return;
    }

    generate.disabled = true;
    status.textContent = "Generating natural voice...";

    player.removeAttribute("src");
    player.load();
    download.style.display = "none";

    try {

        const response = await fetch("/generate", {
            method:"POST",
            headers:{
                "Content-Type":"application/json"
            },
            body:JSON.stringify({
                text:value,
                voice:voice.value,
                rate:rate.value,
                pitch:"+0Hz"
            })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "Generation failed");
        }

        player.src = data.url;
        player.load();

        download.href = data.url;
        download.style.display = "block";

        status.textContent = "Voice generated successfully.";

    } catch(error) {

        status.textContent = error.message;

    } finally {

        generate.disabled = false;

    }
};

</script>

</body>
</html>
""")


@app.post("/generate")
async def generate_voice(request: VoiceRequest):

    text = request.text.strip()

    if not text:
        raise HTTPException(
            status_code=400,
            detail="Text is empty."
        )

    if len(text) > 5000:
        raise HTTPException(
            status_code=400,
            detail="Text is too long. Maximum is 5000 characters."
        )

    filename = f"{uuid.uuid4().hex}.mp3"
    output = AUDIO_DIR / filename

    try:

        communicate = Communicate(
            text=text,
            voice=request.voice,
            rate=request.rate,
            pitch=request.pitch
        )

        await communicate.save(str(output))

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Voice generation failed: {str(e)}"
        )

    return {
        "success": True,
        "url": f"/audio/{filename}"
    }


@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


@app.get("/audio/{filename}")
async def audio_file(filename: str):

    file = AUDIO_DIR / filename

    if not file.exists():
        raise HTTPException(
            status_code=404,
            detail="Audio not found."
        )

    return FileResponse(
        file,
        media_type="audio/mpeg"
    )


if __name__ == "__main__":

    import uvicorn

    port = int(os.environ.get("PORT", 10000))

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port
    )
