#!/usr/bin/env python3
"""Convert local audio to Tencent SILK using afconvert and isolated pilk 0.2.4."""
import argparse,json,pathlib,subprocess,tempfile,wave
import pilk

def encode(source,output):
    source=pathlib.Path(source).resolve();output=pathlib.Path(output).resolve()
    if source==output:raise ValueError('Input and output must differ')
    with tempfile.TemporaryDirectory(prefix='rea-voice-codec-') as temporary:
        temp=pathlib.Path(temporary);wav=temp/'audio.wav';pcm=temp/'audio.pcm';decoded=temp/'decoded.pcm';silk=temp/'audio.silk'
        subprocess.run(['/usr/bin/afconvert','-f','WAVE','-d','LEI16@16000','-c','1',str(source),str(wav)],check=True,capture_output=True)
        with wave.open(str(wav),'rb') as audio:
            assert audio.getframerate()==16000 and audio.getnchannels()==1 and audio.getsampwidth()==2
            pcm.write_bytes(audio.readframes(audio.getnframes()))
        # pilk returns truncated seconds. Derive exact ms from decoded samples.
        pilk.encode(str(pcm),str(silk),pcm_rate=16000,tencent=True)
        pilk.decode(str(silk),str(decoded),pcm_rate=16000)
        duration=decoded.stat().st_size*1000//32000
        if not 1000<=duration<=60000:raise ValueError('Adapter accepts 1–60 seconds')
        data=silk.read_bytes()
        if not data.startswith(b'\x02#!SILK_V3'):raise ValueError('Unexpected codec output')
        output.write_bytes(data)
    return {'file':str(output),'duration_ms':duration,'bytes':len(data),'sample_rate':16000,'codec':'pilk 0.2.4'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    print(json.dumps(encode(args.input,args.output),ensure_ascii=False,indent=2))
