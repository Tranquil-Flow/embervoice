const $ = id => document.getElementById(id);
let book = null, job = null, timer = null, clockTimer = null, presets = {}, reference = null, referenceUploading = false;
let recording = null, capturePending = false, captureEpoch = 0, recordedPreviewUrl = null;

const stored = {get(key){try{return localStorage.getItem('studio.'+key)}catch{return null}}, set(key,value){try{localStorage.setItem('studio.'+key,value)}catch{}}};
const show = (id, visible) => $(id).classList.toggle('hidden', !visible);
function notice(message=''){$('notice').textContent=message}
async function api(path, options={}) {
  const response = await fetch(path, options);
  if(!response.ok){let message=`Request failed (${response.status})`;try{message=(await response.json()).detail||message}catch{};throw new Error(message)}
  return response.json();
}
function setBook(info){releaseRecordedPreview();$('audio-player').pause();$('audio-player').removeAttribute('src');show('player-panel',false);$('chapter-player').pause();$('chapter-player').removeAttribute('src');show('chapter-listen',false);show('saved-versions',false);book=info;reference=null;$('reference-text').value='';renderReference();restoreReference(info.id);loadVersions(info.id);stored.set('book',info.id);$('book-title').textContent=info.title;$('book-author').textContent=info.author;$('book-stats').textContent=`${info.chapters.length} sections · ${info.language.toUpperCase()} · ${info.chapters.reduce((n,c)=>n+c.passages,0)} passages planned`;
 $('book-cover').className=coverClass(info.title);show('book-panel',true);markShelf();syncButtons();notice('');}
function previewChapter(info){const sample=info.chapters.findIndex(ch=>ch.characters>250);return sample<0?1:sample+1}
function coverClass(title){let hash=0;for(const ch of title||'')hash=(hash*31+ch.codePointAt(0))>>>0;return 'cover c'+(hash%6)}
function markShelf(){document.querySelectorAll('.shelf-book').forEach(row=>{const open=row.dataset.book===book?.id;row.classList.toggle('is-selected',open);if(open)row.setAttribute('aria-current','true');else row.removeAttribute('aria-current')})}
/* shipped samples of each preset as written; they play only when pressed, one at a time, and never while recording */
const samplePlayer=new Audio();samplePlayer.preload='none';let sampleKey=null;
function stopSample(){samplePlayer.pause();sampleKey=null;document.querySelectorAll('.preset-play').forEach(button=>{button.setAttribute('aria-pressed','false');button.firstElementChild.textContent='▶'})}
let funOpen=false;
function showFun(open=funOpen){funOpen=open;$('fun-toggle').setAttribute('aria-pressed',String(open));document.querySelectorAll('.preset-item.fun').forEach(item=>{const keep=open||item.querySelector('input').checked;item.classList.toggle('hidden',!keep);if(!keep&&sampleKey===item.querySelector('.preset-play').dataset.sample)stopSample()})}
function playSample(key){const again=sampleKey===key;stopSample();if(again||recording||capturePending)return;for(const id of ['audio-player','chapter-player','record-playback'])$(id).pause();samplePlayer.src=`/voices/${key}.m4a`;sampleKey=key;const button=document.querySelector(`.preset-play[data-sample="${key}"]`);button.setAttribute('aria-pressed','true');button.firstElementChild.textContent='■';samplePlayer.play().catch(()=>{if(sampleKey===key){stopSample();notice('That voice sample could not be played.')}})}
function renderSummary(){const summary=$('make-summary');summary.replaceChildren();if(!book){summary.textContent='Choose a book to begin.';return}const checked=document.querySelector('input[name="voice"]:checked');const edited=checked&&presets[checked.value]&&$('style').value.trim()!==presets[checked.value].trim();const name=checked?checked.closest('.preset').querySelector('strong').textContent:'Custom direction';const title=document.createElement('b');title.textContent=book.title;summary.append(title,document.createElement('br'),`${name}${edited?' (edited)':''} · ${reference?'with voice clip':checked?.hasAttribute('data-anchor')?'built-in voice':'no voice clip'}`)}

async function refreshLibrary(){try{const data=await api('/api/library'),list=$('library-list');list.replaceChildren();for(const entry of data.books){const row=document.createElement('div');row.className='shelf-book';row.dataset.book=entry.id;const cover=document.createElement('span');cover.className=coverClass(entry.title);cover.setAttribute('aria-hidden','true');const title=document.createElement('b');title.textContent=entry.title;const meta=document.createElement('small');meta.textContent=`${entry.author} · ${entry.versions} voice version${entry.versions===1?'':'s'}`;const button=document.createElement('button');button.className='text-button shelf-open';button.type='button';button.textContent='Open book';button.disabled=Boolean(job&&['running','cancelling'].includes(job.state));button.addEventListener('click',async()=>{if(job&&['running','cancelling'].includes(job.state))return;try{const info=await api(`/api/books/${entry.id}`);if(timer)clearTimeout(timer);stopClock();job=null;stored.set('job','');show('job-panel',false);setBook(info);$('drop-zone').classList.add('hidden')}catch(e){notice(e.message)}});row.append(cover,title,meta,button);list.append(row)}show('library-panel',Boolean(data.books.length));markShelf();syncButtons()}catch(e){notice('Could not load the local shelf: '+e.message)}}

async function loadVersions(bookId){
  try{
    const data=await api(`/api/books/${bookId}/versions`);
    if(book?.id!==bookId)return;
    const list=$('versions-list');list.replaceChildren();
    const finished=data.versions.filter(entry=>entry.complete);
    for(const entry of finished){
      const details=document.createElement('details');details.className='version-entry';
      const summary=document.createElement('summary');summary.textContent=`Voice version ${entry.id.slice(0,8)} · Complete`;
      details.append(summary);
      const complete=document.createElement('a');complete.href=entry.book_url;complete.download='';complete.textContent='↓ Download complete chapter-marked M4B';details.append(complete);
      const chapterList=document.createElement('div');chapterList.className='saved-chapters';
      entry.chapters.forEach((part,index)=>{
        const row=document.createElement('div');row.className='saved-chapter';
        const button=document.createElement('button');button.type='button';button.className='text-button';button.textContent=`Load ${String(index+1).padStart(2,'0')} — ${part.title}`;
        button.addEventListener('click',()=>{const player=$('chapter-player');player.pause();player.src=part.url;player.load();$('playing-chapter').textContent=`${part.title} · press play to listen`;show('chapter-listen',true)});
        const link=document.createElement('a');link.href=part.url;link.download=part.name;link.textContent='↓ Download';row.append(button,link);chapterList.append(row);
      });
      details.append(chapterList);

      list.append(details);
    }
    show('saved-versions',Boolean(finished.length));syncButtons();
  }catch(e){if(book?.id===bookId)notice('Could not load saved audio: '+e.message)}
}
function syncButtons(){const setupReady=window.STUDIO_READY !== false;const busy=Boolean(job&&['running','cancelling'].includes(job.state))||capturePending||Boolean(recording);const enabled=Boolean(setupReady&&book&&$('style').value.trim().length>=3&&!referenceUploading&&(!reference||$('reference-text').value.trim())&&!busy);$('preview').disabled=!enabled;$('full').disabled=!enabled;$('low-memory-run').disabled=!enabled;$('replace-book').disabled=busy;$('reference-audio').disabled=busy||referenceUploading;$('reference-clear').disabled=busy||referenceUploading;$('record-start').disabled=!book||busy||referenceUploading;$('record-stop').disabled=!recording;document.querySelectorAll('.shelf-open').forEach(button=>button.disabled=busy);document.querySelectorAll('.preset-play').forEach(button=>button.disabled=capturePending||Boolean(recording));renderSummary()}
function renderReference(){show('reference-selected',Boolean(reference));show('reference-transcript',Boolean(reference));$('reference-name').textContent=reference?`${reference.name} · ${reference.duration_seconds} s saved locally`:'';syncButtons()}
async function restoreReference(bookId){const id=stored.get('reference.'+bookId);if(!id)return;try{const found=await api(`/api/books/${bookId}/references/${id}`);if(book?.id!==bookId||stored.get('reference.'+bookId)!==id)return;reference=found;$('reference-text').value=stored.get('reference-text.'+bookId+'.'+id)||'';renderReference()}catch{if(book?.id===bookId&&stored.get('reference.'+bookId)===id){stored.set('reference.'+bookId,'');reference=null;renderReference()}}}
async function uploadReference(file,fromRecording=false){if(!file||!book)return null;if(job&&['running','cancelling'].includes(job.state)){notice('Wait for the current run before changing the voice.');return null}if(file.size>10*1024*1024){notice('Voice clip exceeds the 10 MB limit.');return null}referenceUploading=true;syncButtons();notice('Preparing the voice clip locally…');try{const bookId=book.id;const data=new FormData();data.append('file',file);const found=await api(`/api/books/${bookId}/references`,{method:'POST',body:data});if(book?.id!==bookId)return null;reference=found;$('reference-text').value=stored.get('reference-text.'+bookId+'.'+found.id)||'';stored.set('reference.'+bookId,found.id);if(!fromRecording)releaseRecordedPreview();renderReference();notice('Voice clip ready. Add the exact spoken words to enable generation.');return found}catch(e){notice(e.message);return null}finally{referenceUploading=false;$('reference-audio').value='';syncButtons()}}
function releaseRecordedPreview(){if(recordedPreviewUrl){$('record-playback').pause();$('record-playback').removeAttribute('src');$('record-playback').load();URL.revokeObjectURL(recordedPreviewUrl);recordedPreviewUrl=null}show('record-listen',false)}
function recordingStatus(session){const elapsed=Math.min(30,Math.floor((performance.now()-session.started)/1000));$('record-status').textContent=`Recording 0:${String(elapsed).padStart(2,'0')} / 0:30`}
function stopRecording(discard=false){if(capturePending){captureEpoch++;capturePending=false;syncButtons()}const session=recording;if(!session)return;session.discard=discard;recording=null;clearInterval(session.tick);clearTimeout(session.limit);for(const track of session.stream.getTracks())track.stop();if(session.recorder.state!=='inactive')session.recorder.stop();show('record-start',true);show('record-stop',false);$('record-status').textContent=discard?'Recording discarded':'Preparing recording…';syncButtons()}
async function finishRecording(session){for(const track of session.stream.getTracks())track.stop();if(session.discard||book?.id!==session.bookId)return;const seconds=(performance.now()-session.started)/1000;if(seconds<2){$('record-status').textContent='Recording was too short';notice('Speak for at least 2 seconds, then try again.');return}const mime=session.recorder.mimeType||session.mime;const suffix=mime.includes('webm')?'webm':mime.includes('ogg')?'ogg':mime.includes('mp4')?'m4a':null;if(!suffix){notice('This browser cannot provide a supported audio recording. Choose a clip file instead.');return}const blob=new Blob(session.chunks,{type:mime});if(!blob.size||blob.size>10*1024*1024){notice('Recording was empty or exceeded 10 MB. Please try again.');return}const file=new File([blob],`My voice recording.${suffix}`,{type:mime});const found=await uploadReference(file,true);if(!found)return;releaseRecordedPreview();recordedPreviewUrl=URL.createObjectURL(blob);$('record-playback').src=recordedPreviewUrl;show('record-listen',true);$('record-status').textContent='Recording saved locally · listen before generating';}
async function startRecording(){if(!book||recording||capturePending||referenceUploading||job&&['running','cancelling'].includes(job.state))return;if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){notice('Microphone recording is unavailable in this browser. Choose a clip file instead.');return}const mime=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(type=>MediaRecorder.isTypeSupported(type));if(!mime){notice('No supported recording format in this browser. Choose a clip file instead.');return}stopSample();const bookId=book.id,epoch=++captureEpoch;capturePending=true;syncButtons();$('record-status').textContent='Requesting microphone permission…';let stream=null;try{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false},video:false});if(epoch!==captureEpoch||book?.id!==bookId||document.hidden){stream.getTracks().forEach(track=>track.stop());return}const media=new MediaRecorder(stream,{mimeType:mime});const session={recorder:media,stream,mime,bookId,chunks:[],started:performance.now(),discard:false};media.ondataavailable=event=>{if(event.data.size)session.chunks.push(event.data)};media.onstop=()=>finishRecording(session);media.onerror=()=>{stopRecording(true);notice('Recording failed. Please try again.')};stream.getAudioTracks().forEach(track=>track.addEventListener('ended',()=>{if(recording===session){stopRecording(true);notice('Microphone connection ended; please try again.')}}));recording=session;media.start();$('record-playback').pause();show('record-listen',false);show('record-start',false);show('record-stop',true);recordingStatus(session);session.tick=setInterval(()=>recordingStatus(session),250);session.limit=setTimeout(()=>stopRecording(),29500);syncButtons()}catch(e){if(recording)stopRecording(true);else stream?.getTracks().forEach(track=>track.stop());notice(e?.name==='NotAllowedError'?'Microphone access was denied. You can allow it in your browser settings or choose a clip file.':'Microphone access failed. Choose a clip file or try again.');$('record-status').textContent='Not recording'}finally{capturePending=false;syncButtons()}}
function updateClock(){if(!job||!['running','cancelling'].includes(job.state))return;const elapsed=Math.max(0,Math.floor(Date.now()/1000-job.started_at));$('work-elapsed').textContent=`Elapsed ${Math.floor(elapsed/60)}:${String(elapsed%60).padStart(2,'0')}`}
function stopClock(){if(clockTimer){clearInterval(clockTimer);clockTimer=null}}
function renderJob(data){const wasActive=job&&['running','cancelling'].includes(job.state);data.started_at=data.started_at||job?.started_at||Date.now()/1000;job=data;show('job-panel',true);show('cancel',data.state==='running');
 const names={running:data.mode==='preview'?'Finding the voice…':'Narrating your book…',cancelling:'Stopping safely…',complete:data.mode==='preview'?'Your preview is ready':'Audiobook complete',cancelled:'Run stopped',error:'Something went wrong'};
 $('job-state').textContent=data.error_code==='low_memory'?'More memory recommended':names[data.state]||data.state;
 const active=['running','cancelling'].includes(data.state);show('work-signal',active);show('low-memory-badge',active&&Boolean(data.low_memory));show('low-memory-run',data.state==='error'&&data.error_code==='low_memory');$('work-signal').classList.toggle('is-working',data.state==='running');lightCandle(data.state==='running');
 $('work-stage').textContent=data.state==='cancelling'?'Stopping the current worker…':data.stage_message||'Preparing local narration…';
 if(active){updateClock();if(!clockTimer)clockTimer=setInterval(updateClock,1000)}else stopClock();
 const total=book?book.chapters.reduce((n,c)=>n+c.passages,0):0;
 const previewSaved=Boolean(data.preview_url),saved=data.mode==='preview'?(previewSaved?1:0):data.passages_done;feedHearth(data,saved);
 $('job-count').textContent=data.mode==='preview'?`${saved} / 1 preview passage`:`${saved} / ${total} passages`;
 const percent=data.mode==='preview'?(previewSaved?100:0):(total?Math.min(100,Math.round(100*saved/total)):0);
 const track=$('progress-fill').parentElement;
 $('progress-fill').style.width=`${percent}%`;track.setAttribute('aria-valuenow',String(percent));
 track.setAttribute('aria-valuetext',`${saved} of ${data.mode==='preview'?1:total} passages saved`);
 track.classList.toggle('is-working',data.state==='running');
 let progressNote=`${percent}% of passages saved`;
 if(data.state==='running'&&data.mode==='preview')progressNote='Preview: one passage · time remaining unknown';
 else if(data.state==='running'&&data.phase==='assembling')progressNote='All passages saved · assembling the M4B (ETA unavailable)';
 else if(data.state==='running'&&data.mode==='full'){
   const seconds=data.eta_narration_seconds;
   progressNote=Number.isFinite(seconds)&&seconds>0
     ?`Rough narration ETA: ~${Math.max(1,Math.ceil(seconds/60))} min remaining · ${data.eta_samples} measured passages; encoding extra`
     :'Estimating after two new passages · saved passage progress is exact';
 }
 $('progress-caption').textContent=progressNote;
 $('job-detail').textContent=data.error|| (data.state==='running'?`Completed ${data.chapters_done} chapters. You can leave this page open; generated passages are saved as they finish.`:data.state==='cancelled'?'Finished passages remain on disk. Start again with the same voice direction to resume.':data.mode==='preview'?'Listen below, then generate the full book when ready.':'Each chapter and the combined M4B are ready.');
 $('job-log').textContent=(data.log||[]).join('\n');
 show('player-panel',data.mode==='preview'&&Boolean(data.preview_url));if(data.mode==='preview'&&data.preview_url&&$('audio-player').getAttribute('src')!==data.preview_url)$('audio-player').src=data.preview_url;else if(data.mode!=='preview')$('audio-player').pause();
 show('downloads',Boolean(data.chapters.length||data.book_url));const links=$('download-links');links.replaceChildren();
 if(data.book_url){const link=document.createElement('a');link.href=data.book_url;link.download='';link.textContent='↓ Download complete M4B';links.append(link)}
 data.chapters.forEach(ch=>{const link=document.createElement('a');link.href=ch.url;link.download='';link.textContent='↓ '+ch.name;links.append(link)});
 if(!['running','cancelling'].includes(data.state)&&(wasActive||data.id!==renderJob.lastFinished)){renderJob.lastFinished=data.id;if(book){loadVersions(book.id);refreshLibrary()}}
 syncButtons();}
async function poll(){if(!job)return;try{const data=await api('/api/jobs/'+job.id);renderJob(data);if(['running','cancelling'].includes(data.state)){timer=setTimeout(poll,1200)}else{timer=null;if(data.state==='error'&&data.error_code!=='low_memory')notice(data.error||'Conversion failed')}}catch(e){timer=null;stopClock();if(e.message.includes('Unknown job')){job=null;stored.set('job','');show('job-panel',false);syncButtons()}notice(e.message+' — If the server restarted, submit the same book and voice direction to resume.')}}
async function upload(file){if(!file)return;if(recording||capturePending){notice('Stop recording before changing books.');return}if(job&&['running','cancelling'].includes(job.state)){notice('Stop the current run before changing books.');return}if(!file.name.toLowerCase().endsWith('.epub')){notice('Please choose an EPUB file.');return}if(file.size>50*1024*1024){notice('EPUB exceeds the 50 MB limit.');return}
 notice('Inspecting your book locally…');$('drop-zone').setAttribute('aria-busy','true');try{const data=new FormData();data.append('file',file);const info=await api('/api/books',{method:'POST',body:data});if(timer)clearTimeout(timer);stopClock();job=null;show('job-panel',false);setBook(info);refreshLibrary();$('drop-zone').classList.add('hidden');stored.set('job','');}catch(e){notice(e.message)}finally{$('drop-zone').removeAttribute('aria-busy')}}
async function start(mode,lowMemory=false){if(!book)return;const transcript=$('reference-text').value.trim();if(reference&&!transcript){notice('Enter the complete, exact words spoken in the voice clip first.');$('reference-text').focus();return}notice('');try{const chapter=mode==='preview'?previewChapter(book):1;const data=await api('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({book_id:book.id,style:$('style').value.trim(),mode,chapter,reference_id:reference?.id||null,reference_text:reference?transcript:null,preset:document.querySelector('input[name="voice"]:checked')?.value||null,allow_low_memory:lowMemory})});if(timer)clearTimeout(timer);renderJob(data);stored.set('job',data.id);poll();if(matchMedia('(max-width: 700px)').matches)$('job-panel').scrollIntoView({block:'nearest',behavior:reducedMotion.matches?'auto':'smooth'})}catch(e){notice(e.message)}}
async function init(){startHearth();try{presets=await api('/api/presets')}catch(e){notice('Local server unavailable: '+e.message);return}
 const savedStyle=stored.get('style');$('style').value=savedStyle||presets.warm;const savedPreset=stored.get('preset');if(savedPreset&&document.querySelector(`input[name="voice"][value="${savedPreset}"]`))document.querySelector(`input[name="voice"][value="${savedPreset}"]`).checked=true;
 document.querySelectorAll('input[name="voice"]').forEach(r=>r.addEventListener('change',()=>{if(r.checked){$('style').value=presets[r.value];stored.set('style',$('style').value);stored.set('preset',r.value);showFun();syncButtons()}}));
 $('fun-toggle').addEventListener('click',()=>{showFun(!funOpen);stored.set('fun',funOpen?'1':'')});showFun(stored.get('fun')==='1');
 document.querySelectorAll('.preset-play').forEach(button=>button.addEventListener('click',()=>playSample(button.dataset.sample)));samplePlayer.addEventListener('ended',stopSample);
 for(const id of ['audio-player','chapter-player','record-playback'])$(id).addEventListener('play',stopSample);
 $('style').addEventListener('input',()=>{stored.set('style',$('style').value);syncButtons()});

 $('reference-audio').addEventListener('change',event=>uploadReference(event.target.files[0]));
 $('record-start').addEventListener('click',startRecording);$('record-stop').addEventListener('click',()=>stopRecording());
 window.addEventListener('pagehide',()=>{stopSample();captureEpoch++;stopRecording(true);releaseRecordedPreview()});
 document.addEventListener('visibilitychange',()=>{if(document.hidden){captureEpoch++;stopRecording(true)}});
 $('reference-text').addEventListener('input',()=>{if(book&&reference)stored.set('reference-text.'+book.id+'.'+reference.id,$('reference-text').value);syncButtons()});
 $('reference-clear').addEventListener('click',()=>{if(recording||capturePending||job&&['running','cancelling'].includes(job.state))return;reference=null;releaseRecordedPreview();$('reference-text').value='';$('reference-audio').value='';if(book)stored.set('reference.'+book.id,'');renderReference();notice('Voice clip removed from this selection; older versions stay on disk for safe resume.')});
 $('file-picker').addEventListener('change',event=>upload(event.target.files[0]));
 $('drop-zone').addEventListener('click',()=>$('file-picker').click());$('drop-zone').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('file-picker').click()}});
 for(const type of ['dragenter','dragover'])$('drop-zone').addEventListener(type,e=>{e.preventDefault();$('drop-zone').classList.add('dragging')});
 for(const type of ['dragleave','drop'])$('drop-zone').addEventListener(type,e=>{e.preventDefault();$('drop-zone').classList.remove('dragging')});
 $('drop-zone').addEventListener('drop',e=>upload(e.dataTransfer.files[0]));
 $('replace-book').addEventListener('click',()=>$('file-picker').click());$('preview').addEventListener('click',()=>start('preview'));$('low-memory-run').addEventListener('click',()=>{if(job?.error_code==='low_memory')start(job.mode,true)});$('full').addEventListener('click',()=>start('full'));
 $('cancel').addEventListener('click',async()=>{try{if(timer)clearTimeout(timer);renderJob(await api('/api/jobs/'+job.id+'/cancel',{method:'POST'}));poll()}catch(e){notice(e.message)}});
 const bookId=stored.get('book');if(bookId){try{setBook(await api('/api/books/'+bookId));$('drop-zone').classList.add('hidden')}catch{stored.set('book','')}}
 const jobId=stored.get('job');if(jobId&&book){job={id:jobId,state:'running'};poll()}
 refreshLibrary();
 syncButtons();}

/* Hearth: the fire level is a calm-to-inferno preference. The candle, the stoke, sparks and rising words follow only real job state:
   the candle burns only while a run is underway, and each spark burst and word flurry marks a passage that is already saved on disk. */
const BURN_NAMES=['Embers','Hearth','Bonfire','Blaze','Inferno'];
const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)'),darkScheme=matchMedia('(prefers-color-scheme: dark)');
const fire={cv:null,g:null,ps:[],sp:[],eb:null,b:0,flash:0,acc:0,burn:.3,still:false,last:0,heat:null};
const hearth={jobId:null,saved:0,pool:[]};
const working=()=>Boolean(job&&job.state==='running');
function setBurn(value,save=true){const v=Math.max(0,Math.min(1,value));fire.burn=v;document.documentElement.style.setProperty('--burn',String(v));if(reducedMotion.matches)setHeat(v);const name=BURN_NAMES[Math.min(4,Math.floor(v*4.999))];$('burn').value=String(Math.round(v*100));$('burn').setAttribute('aria-valuetext',name);$('burn-name').textContent=name;fire.still=false;if(save)stored.set('burn',String(v))}
/* the glow, candle and progress sheen follow the stoked heat, so a run lifts even Inferno past the slider's top */
function setHeat(value){const q=Math.round(value*50)/50;if(q!==fire.heat){fire.heat=q;document.documentElement.style.setProperty('--heat',String(q))}}
function lightCandle(lit){const candle=$('make-candle');if(candle.classList.contains('is-lit')&&!lit){candle.classList.add('is-snuffed');setTimeout(()=>candle.classList.remove('is-snuffed'),3200)}candle.classList.toggle('is-lit',lit)}
function sprite(stops){const c=document.createElement('canvas');c.width=c.height=64;const g=c.getContext('2d'),r=g.createRadialGradient(32,32,0,32,32,32);stops.forEach(([o,col])=>r.addColorStop(o,col));g.fillStyle=r;g.fillRect(0,0,64,64);return c}
let SPRITES=null;
function sprites(){return SPRITES||(SPRITES={dark:[sprite([[0,'rgba(255,250,220,1)'],[.3,'rgba(255,220,120,.6)'],[1,'rgba(255,160,40,0)']]),sprite([[0,'rgba(255,170,60,.9)'],[.4,'rgba(255,110,20,.45)'],[1,'rgba(220,50,0,0)']]),sprite([[0,'rgba(200,50,10,.6)'],[.5,'rgba(120,20,0,.25)'],[1,'rgba(60,0,0,0)']])],
 light:[sprite([[0,'rgba(255,210,90,.9)'],[.4,'rgba(255,170,60,.45)'],[1,'rgba(255,140,40,0)']]),sprite([[0,'rgba(240,110,30,.75)'],[.45,'rgba(225,80,20,.35)'],[1,'rgba(200,50,10,0)']]),sprite([[0,'rgba(170,40,20,.45)'],[.5,'rgba(140,30,10,.18)'],[1,'rgba(120,20,0,0)']])],
 spark:sprite([[0,'rgba(255,240,200,1)'],[.25,'rgba(255,170,60,.8)'],[1,'rgba(255,120,30,0)']])})}
function burst(count){const W=fire.cv.width,H=fire.cv.height;for(let i=0;i<count;i++)fire.sp.push({x:Math.random()*W,y:H-10,vx:(Math.random()-.5)*2.2,vy:-(2.5+Math.random()*4.5),age:0,life:70+Math.random()*90,seed:Math.random()*6.28,r:1.4+Math.random()*1.8});fire.flash=1}
function stepFire(dt){const target=fire.burn+(working()?.22:0);fire.eb=fire.eb==null?target:fire.eb+(target-fire.eb)*Math.min(1,.02*dt);
 const b=Math.min(1.22,fire.eb),W=fire.cv.width,H=fire.cv.height,ts=(.3+b*.95)*dt;fire.b=b;setHeat(b);fire.flash=Math.max(0,fire.flash-.02*dt);
 fire.acc+=(3+b*16)*ts;let n=Math.floor(fire.acc);fire.acc-=n;n=Math.min(n,Math.max(0,(b>1?900:700)-fire.ps.length));
 for(let i=0;i<n;i++){const log=(Math.floor(Math.random()*5)+.5)/5;fire.ps.push({x:(log+(Math.random()-.5)*.26)*W,y:H+8,vx:(Math.random()-.5)*.6,vy:-(.5+b*1.5+Math.random()*.9),age:0,life:30+b*42+Math.random()*20,r:8+b*16+Math.random()*8,seed:Math.random()*6.28})}
 if(Math.random()<(.03+b*.5)*ts)fire.sp.push({x:Math.random()*W,y:H,vx:(Math.random()-.5)*.8,vy:-(.6+Math.random()*1.2+b*1.6),age:0,life:120+Math.random()*200,seed:Math.random()*6.28,r:1.2+Math.random()*1.4+b});
 fire.ps=fire.ps.filter(p=>(p.age+=ts)<p.life);fire.sp=fire.sp.filter(p=>(p.age+=dt*.9)<p.life&&p.y>-10);
 for(const p of fire.ps){const t=p.age/p.life;p.x+=(p.vx+Math.sin(p.seed+p.age*.12)*.45*(1-t))*ts;p.y+=p.vy*(1-t*.35)*ts}
 for(const p of fire.sp){p.x+=(p.vx+Math.sin(p.age*.05+p.seed)*.3)*dt*.9;p.y+=p.vy*dt*.9;p.vy*=Math.pow(.995,dt)}}
function drawFire(){const {g,cv}=fire,W=cv.width,H=cv.height,b=Math.min(1,fire.b),over=Math.max(0,fire.b-1),dark=darkScheme.matches,all=sprites(),spr=dark?all.dark:all.light;
 g.clearRect(0,0,W,H);g.globalCompositeOperation=dark?'lighter':'source-over';
 const bh=30+b*60+over*90+fire.flash*40,bed=g.createLinearGradient(0,H,0,H-bh);bed.addColorStop(0,dark?`rgba(255,90,20,${.35+b*.45+fire.flash*.3})`:`rgba(230,90,30,${.18+b*.35+fire.flash*.2})`);bed.addColorStop(1,'rgba(255,120,30,0)');g.fillStyle=bed;g.fillRect(0,H-bh,W,bh);
 for(const p of fire.ps){const t=p.age/p.life,s=spr[t<.28?0:t<.62?1:2],r=p.r*(1+over*1.1)*(1-t*.55);g.globalAlpha=Math.min(1,(1-t)*1.6)*(t<.08?t/.08:1)*(dark?.5*(1-b*.4):.42*(1-b*.45));g.drawImage(s,p.x-r,p.y-r*1.25,r*2,r*2.5)}
 for(const p of fire.sp){const t=p.age/p.life,r=p.r*3;g.globalAlpha=Math.max(0,(1-t)*(.6+.4*Math.sin(p.age*.3)));g.drawImage(all.spark,p.x-r,p.y-r,r*2,r*2)}
 g.globalAlpha=1}
function fireFrame(now){if(reducedMotion.matches){if(!fire.still){fire.ps=[];fire.sp=[];fire.eb=null;for(let i=0;i<90;i++)stepFire(1);fire.sp=[];drawFire();fire.still=true}fire.last=0}
 else{const dt=fire.last?Math.min(3,(now-fire.last)/16.67):1;fire.last=now;fire.still=false;stepFire(dt);drawFire()}requestAnimationFrame(fireFrame)}
/* words rising from passages already saved to disk; amount and speed follow the fire level the user chose */
const STOPWORDS=new Set('that this with from have been were they their them what when which while would could should there then than your into about such some other must just made very also only upon said will shall more most much many being does done here each every like over even after before because these those whom whose where how all and the for are but not you any can had her his its one our out was who may say she him own too yet nor'.split(' '));
const wordsOf=text=>(text.match(/[\p{L}’']{4,}/gu)||[]).map(word=>word.replace(/[’']s$/,'')).filter(word=>word.length>=4&&!STOPWORDS.has(word.toLowerCase()));
const chapterWordCache=new Map();
function chapterWords(bookId,chapter){const key=bookId+':'+chapter;if(!chapterWordCache.has(key))chapterWordCache.set(key,fetch(`/api/books/${bookId}/chapters/${chapter}/passages`).then(r=>r.ok?r.json():{passages:[]}).then(data=>data.passages.map(p=>wordsOf(p.text||''))).catch(()=>[]));return chapterWordCache.get(key)}
function locatePassage(info,index){let left=index;for(let i=0;i<info.chapters.length;i++){if(left<info.chapters[i].passages)return {chapter:i+1,index:left};left-=info.chapters[i].passages}return null}
async function gatherWords(data,saved,flurry){const info=book;if(!info||info.id!==data.book_id)return;const last=locatePassage(info,saved-1);if(!last)return;const pool=[];let newest=[];
 for(let chapter=Math.max(1,last.chapter-2);chapter<=last.chapter;chapter++){const lists=await chapterWords(info.id,chapter);if(book!==info||hearth.jobId!==data.id)return;const upto=chapter===last.chapter?last.index+1:lists.length;lists.slice(0,upto).forEach(words=>pool.push(...words));if(chapter===last.chapter)newest=lists[last.index]||[]}
 hearth.pool=pool;if(flurry)wordFlurry(newest)}
function wordFlurry(words){const b=fire.burn,picks=[...words].sort(()=>Math.random()-.5).slice(0,Math.round(4+b*14));picks.forEach((word,i)=>setTimeout(()=>riseWord(word,true),i*(200-b*120)+Math.random()*120))}
/* a preview narrates passage 1 of its sample section, so its words come from the passage being voiced */
async function previewWords(data,flurry){const info=book;if(!info||info.id!==data.book_id)return;const lists=await chapterWords(info.id,previewChapter(info));if(book!==info||hearth.jobId!==data.id)return;hearth.pool=lists[0]||[];if(flurry)wordFlurry(hearth.pool)}
function riseWord(word,fromBottom){if(reducedMotion.matches||document.hidden)return;const el=document.createElement('span');el.className='smokeword';el.textContent=word;el.setAttribute('aria-hidden','true');
 const make=document.querySelector('.make').getBoundingClientRect(),right=make.left>innerWidth*.45?make.left-24:innerWidth-16,size=12+Math.random()*9,room=Math.max(40,right-24-word.length*size*.5);
 el.style.fontSize=size+'px';el.style.left=(24+Math.random()*room)+'px';el.style.top=((fromBottom?.78+Math.random()*.14:.5+Math.random()*.38)*innerHeight)+'px';fire.cv.after(el);
 const b=fire.burn,dx=(Math.random()-.5)*60,dy=-(150+b*90+Math.random()*130),peak=(darkScheme.matches?.62:.5)*(.75+Math.random()*.25),duration=(8200-b*4600)+Math.random()*2200;
 el.animate([{opacity:0,transform:'translate(0,0) rotate(0)',filter:'blur(1px)'},{opacity:peak,offset:.18,filter:'blur(0)'},{opacity:peak*.8,offset:.6,transform:`translate(${dx*.6}px,${dy*.6}px) rotate(${dx/14}deg)`,filter:'blur(0)'},{opacity:0,transform:`translate(${dx}px,${dy}px) rotate(${dx/8}deg)`,filter:'blur(3px)'}],{duration,easing:'cubic-bezier(.2,.4,.4,1)'}).onfinish=()=>el.remove()}
function feedHearth(data,saved){const same=hearth.jobId===data.id,grew=same&&saved>hearth.saved;if(!same)hearth.pool=[];hearth.jobId=data.id;hearth.saved=saved;
 if(grew&&!reducedMotion.matches)burst(36);
 if(data.mode==='preview'){if(!same||grew)previewWords(data,grew)}
 else if(saved>0&&(grew||!same))gatherWords(data,saved,grew)}
function ambientWords(){const b=fire.burn;if(working()&&hearth.pool.length)riseWord(hearth.pool[Math.floor(Math.random()*hearth.pool.length)],false);setTimeout(ambientWords,(1100-b*920)*(.75+Math.random()*.5))}
function startHearth(){fire.cv=$('fire');fire.g=fire.cv.getContext('2d');const size=()=>{fire.cv.width=innerWidth;fire.cv.height=innerHeight;fire.still=false};size();addEventListener('resize',size);
 const savedBurn=parseFloat(stored.get('burn'));setBurn(Number.isFinite(savedBurn)?savedBurn:.3,false);$('burn').addEventListener('input',()=>setBurn(Number($('burn').value)/100));
 darkScheme.addEventListener('change',()=>{fire.still=false});reducedMotion.addEventListener('change',()=>{fire.still=false});
 requestAnimationFrame(fireFrame);ambientWords()}
window.addEventListener('studio-readiness',syncButtons);
init();
