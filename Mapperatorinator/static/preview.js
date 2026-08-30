(() => {
    'use strict';

    const bootstrap = window.PREVIEW_BOOTSTRAP || {};
    const csrfHeaders = {
        [bootstrap.csrfHeaderName || 'X-Mapperatorinator-CSRF-Token']: bootstrap.csrfToken || ''
    };
    const timeline = document.getElementById('density-timeline');
    const timelineContext = timeline.getContext('2d');
    const playfield = document.getElementById('playfield');
    const playfieldContext = playfield.getContext('2d');
    const audio = document.getElementById('preview-audio');
    const autoPlayUpdates = document.getElementById('auto-play-updates');
    const channel = typeof BroadcastChannel === 'function'
        ? new BroadcastChannel('mapperatorinpainter-preview')
        : null;
    const comboColors = ['#62c8ff', '#ff638f', '#b8ef65', '#ffc85c'];

    let state = null;
    let scene = null;
    let currentTime = 0;
    let lastMapKey = null;
    let lastMapIdentity = null;
    let currentAudioUrl = null;
    let timelineDragging = false;
    let pollTimer = null;
    let toastTimer = null;
    let danserLaunching = false;
    let danserRunning = false;
    let audioWasUsed = false;

    function formatTimestamp(milliseconds) {
        const value = Math.max(0, Math.round(Number(milliseconds) || 0));
        const minutes = Math.floor(value / 60000);
        const seconds = Math.floor((value % 60000) / 1000);
        const ms = value % 1000;
        return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
    }

    function clamp(value, lower, upper) {
        return Math.max(lower, Math.min(value, upper));
    }

    async function post(path, data = {}) {
        const body = new URLSearchParams();
        Object.entries(data).forEach(([key, value]) => body.set(key, value));
        const response = await fetch(path, {
            method: 'POST',
            headers: { ...csrfHeaders, 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
            body
        });
        const payload = await response.json();
        if (!response.ok || payload.status === 'error') {
            throw new Error(payload.message || `Request failed (${response.status}).`);
        }
        return payload;
    }

    function showToast(message, error = false) {
        const toast = document.getElementById('toast');
        toast.textContent = message;
        toast.classList.toggle('error', error);
        toast.classList.add('visible');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toast.classList.remove('visible'), 2600);
    }

    function setStatus(text, kind = 'waiting') {
        const pill = document.getElementById('connection-status');
        pill.textContent = text;
        pill.className = `status-pill ${kind}`;
    }

    function setControlsEnabled(hasMap) {
        const embeddedReady = hasMap && state?.map?.mode === 0 && Boolean(scene);
        document.getElementById('play-pause-button').disabled = !embeddedReady;
        document.getElementById('jump-selection-button').disabled = !embeddedReady;
        document.getElementById('copy-start-button').disabled = !hasMap;
        document.getElementById('copy-end-button').disabled = !hasMap;
        document.getElementById('danser-button').disabled =
            !embeddedReady || !state?.danser_available || danserLaunching || state?.generating;
        document.getElementById('stop-danser-button').disabled = !danserRunning;
    }

    function updatePlayButton() {
        document.getElementById('play-pause-button').textContent = audio.paused ? 'Play' : 'Pause';
    }

    function resizeCanvas(canvas, context) {
        const rect = canvas.getBoundingClientRect();
        const ratio = window.devicePixelRatio || 1;
        const width = Math.max(1, Math.round(rect.width * ratio));
        const height = Math.max(1, Math.round(rect.height * ratio));
        if (canvas.width !== width || canvas.height !== height) {
            canvas.width = width;
            canvas.height = height;
            context.setTransform(1, 0, 0, 1, 0, 0);
        }
    }

    function resizeCanvases() {
        resizeCanvas(timeline, timelineContext);
        resizeCanvas(playfield, playfieldContext);
        drawTimeline();
        drawPlayfield();
    }

    function drawTimeline() {
        const width = timeline.width;
        const height = timeline.height;
        timelineContext.clearRect(0, 0, width, height);
        if (!state?.has_session || !state.map) return;

        const length = state.map.length_ms;
        const scaleX = (time) => clamp(time / length * width, 0, width);
        const center = height / 2;
        const density = state.map.density || [];

        timelineContext.fillStyle = '#0b0e14';
        timelineContext.fillRect(0, 0, width, height);

        const previewStart = Math.max(0, state.selection.start_time - state.selection.padding_before);
        const previewEnd = Math.min(length, state.selection.end_time + state.selection.padding_after);
        timelineContext.fillStyle = 'rgba(97, 217, 255, 0.10)';
        timelineContext.fillRect(scaleX(previewStart), 0, scaleX(previewEnd) - scaleX(previewStart), height);

        timelineContext.fillStyle = 'rgba(255, 79, 112, 0.20)';
        timelineContext.fillRect(
            scaleX(state.selection.start_time),
            0,
            scaleX(state.selection.end_time) - scaleX(state.selection.start_time),
            height
        );

        const barWidth = density.length ? width / density.length : width;
        timelineContext.fillStyle = '#7f8ba0';
        density.forEach((value, index) => {
            const amplitude = Math.max(1, value * height * 0.42);
            timelineContext.fillRect(
                index * barWidth,
                center - amplitude,
                Math.max(1, barWidth * 0.72),
                amplitude * 2
            );
        });

        timelineContext.strokeStyle = 'rgba(255, 255, 255, 0.14)';
        timelineContext.lineWidth = Math.max(1, window.devicePixelRatio || 1);
        timelineContext.beginPath();
        timelineContext.moveTo(0, center);
        timelineContext.lineTo(width, center);
        timelineContext.stroke();

        const cursorX = scaleX(currentTime);
        timelineContext.strokeStyle = '#ffffff';
        timelineContext.lineWidth = Math.max(2, 2 * (window.devicePixelRatio || 1));
        timelineContext.beginPath();
        timelineContext.moveTo(cursorX, 0);
        timelineContext.lineTo(cursorX, height);
        timelineContext.stroke();
    }

    function playfieldTransform() {
        const ratio = window.devicePixelRatio || 1;
        const width = playfield.width / ratio;
        const height = playfield.height / ratio;
        const scale = Math.min((width - 36) / 512, (height - 28) / 384);
        return {
            ratio,
            width,
            height,
            scale,
            left: (width - 512 * scale) / 2,
            top: (height - 384 * scale) / 2
        };
    }

    function pointOnPath(path, progress) {
        if (!path?.length) return [256, 192];
        const scaled = clamp(progress, 0, 1) * (path.length - 1);
        const before = Math.floor(scaled);
        const after = Math.min(path.length - 1, before + 1);
        const fraction = scaled - before;
        return [
            path[before][0] + (path[after][0] - path[before][0]) * fraction,
            path[before][1] + (path[after][1] - path[before][1]) * fraction
        ];
    }

    function objectAlpha(object, now, preempt) {
        const end = object.end_time ?? object.time;
        if (now < object.time - preempt || now > end + 260) return 0;
        if (now < object.time) {
            return clamp((preempt - (object.time - now)) / Math.min(420, preempt), 0.08, 1);
        }
        if (now <= end) return 1;
        return clamp(1 - (now - end) / 260, 0, 1);
    }

    function drawHitCircle(context, x, y, radius, color, combo, alpha, lead, preempt) {
        context.save();
        context.globalAlpha = alpha;
        if (lead > 0) {
            const approachScale = 1 + 2 * clamp(lead / preempt, 0, 1);
            context.strokeStyle = color;
            context.lineWidth = 2.6;
            context.beginPath();
            context.arc(x, y, radius * approachScale, 0, Math.PI * 2);
            context.stroke();
        }
        context.fillStyle = color;
        context.strokeStyle = '#ffffff';
        context.lineWidth = 3.5;
        context.beginPath();
        context.arc(x, y, radius, 0, Math.PI * 2);
        context.fill();
        context.stroke();
        context.fillStyle = '#171a21';
        context.font = `700 ${Math.max(12, radius * 0.72)}px "Segoe UI", sans-serif`;
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(String(combo), x, y + 0.5);
        context.restore();
    }

    function drawSlider(context, object, now, transform, color, alpha) {
        const path = object.path || [];
        if (path.length < 2) return;
        const radius = scene.circle_radius * transform.scale;
        context.save();
        context.globalAlpha = alpha;
        context.lineCap = 'round';
        context.lineJoin = 'round';
        context.beginPath();
        path.forEach((point, index) => {
            const x = transform.left + point[0] * transform.scale;
            const y = transform.top + point[1] * transform.scale;
            if (index === 0) context.moveTo(x, y);
            else context.lineTo(x, y);
        });
        context.strokeStyle = 'rgba(240, 244, 251, 0.82)';
        context.lineWidth = radius * 2 + 7;
        context.stroke();
        context.strokeStyle = color;
        context.lineWidth = radius * 2;
        context.globalAlpha = alpha * 0.55;
        context.stroke();
        context.restore();

        if (now >= object.time && now <= object.end_time) {
            const spans = Math.max(1, object.repeat || 1);
            const overall = clamp((now - object.time) / Math.max(1, object.end_time - object.time), 0, 1) * spans;
            const spanIndex = Math.min(spans - 1, Math.floor(overall));
            let progress = overall - spanIndex;
            if (spanIndex % 2 === 1) progress = 1 - progress;
            const point = pointOnPath(path, progress);
            const x = transform.left + point[0] * transform.scale;
            const y = transform.top + point[1] * transform.scale;
            context.save();
            context.globalAlpha = alpha;
            context.fillStyle = '#ffffff';
            context.strokeStyle = color;
            context.lineWidth = 4;
            context.beginPath();
            context.arc(x, y, radius * 0.72, 0, Math.PI * 2);
            context.fill();
            context.stroke();
            context.restore();
        }
    }

    function drawSpinner(context, object, now, transform, color, alpha) {
        const x = transform.left + 256 * transform.scale;
        const y = transform.top + 192 * transform.scale;
        const radius = 116 * transform.scale;
        const progress = clamp((now - object.time) / Math.max(1, object.end_time - object.time), 0, 1);
        context.save();
        context.globalAlpha = alpha * 0.82;
        context.fillStyle = 'rgba(8, 10, 15, 0.72)';
        context.strokeStyle = color;
        context.lineWidth = 5;
        context.beginPath();
        context.arc(x, y, radius, 0, Math.PI * 2);
        context.fill();
        context.stroke();
        context.strokeStyle = '#ffffff';
        context.lineWidth = 7;
        context.beginPath();
        context.arc(x, y, radius * 0.78, -Math.PI / 2, -Math.PI / 2 + progress * Math.PI * 2);
        context.stroke();
        context.fillStyle = '#ffffff';
        context.font = `700 ${Math.max(16, 22 * transform.scale)}px "Segoe UI", sans-serif`;
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText('SPIN', x, y);
        context.restore();
    }

    function drawPlayfield() {
        const ratio = window.devicePixelRatio || 1;
        const width = playfield.width / ratio;
        const height = playfield.height / ratio;
        playfieldContext.setTransform(ratio, 0, 0, ratio, 0, 0);
        playfieldContext.clearRect(0, 0, width, height);
        playfieldContext.fillStyle = '#07090d';
        playfieldContext.fillRect(0, 0, width, height);
        if (!scene || !state?.map) return;

        const transform = playfieldTransform();
        playfieldContext.fillStyle = '#10141c';
        playfieldContext.fillRect(transform.left, transform.top, 512 * transform.scale, 384 * transform.scale);
        playfieldContext.strokeStyle = 'rgba(255,255,255,0.10)';
        playfieldContext.lineWidth = 1;
        for (let x = 64; x < 512; x += 64) {
            playfieldContext.beginPath();
            playfieldContext.moveTo(transform.left + x * transform.scale, transform.top);
            playfieldContext.lineTo(transform.left + x * transform.scale, transform.top + 384 * transform.scale);
            playfieldContext.stroke();
        }
        for (let y = 64; y < 384; y += 64) {
            playfieldContext.beginPath();
            playfieldContext.moveTo(transform.left, transform.top + y * transform.scale);
            playfieldContext.lineTo(transform.left + 512 * transform.scale, transform.top + y * transform.scale);
            playfieldContext.stroke();
        }

        const now = currentTime;
        const preempt = scene.approach_preempt;
        const visible = scene.objects
            .filter((object) => objectAlpha(object, now, preempt) > 0)
            .sort((left, right) => right.time - left.time);

        visible.forEach((object) => {
            if (object.type !== 'slider') return;
            const alpha = objectAlpha(object, now, preempt);
            drawSlider(playfieldContext, object, now, transform, comboColors[object.color % comboColors.length], alpha);
        });

        visible.forEach((object) => {
            const alpha = objectAlpha(object, now, preempt);
            const color = comboColors[object.color % comboColors.length];
            if (object.type === 'spinner') {
                drawSpinner(playfieldContext, object, now, transform, color, alpha);
                return;
            }
            if (object.type !== 'circle' && object.type !== 'slider') return;
            const x = transform.left + object.x * transform.scale;
            const y = transform.top + object.y * transform.scale;
            const radius = scene.circle_radius * transform.scale;
            drawHitCircle(playfieldContext, x, y, radius, color, object.combo, alpha, object.time - now, preempt);
        });

        playfieldContext.fillStyle = 'rgba(255,255,255,0.48)';
        playfieldContext.font = '12px "Segoe UI", sans-serif';
        playfieldContext.textAlign = 'right';
        playfieldContext.fillText('lightweight preview', width - 12, height - 10);
    }

    function updateCursor(milliseconds, { seekAudio = false, redraw = true } = {}) {
        if (!state?.map) return;
        currentTime = clamp(Math.round(milliseconds), 0, Math.max(0, state.map.length_ms - 1));
        if (seekAudio && Number.isFinite(audio.duration)) audio.currentTime = currentTime / 1_000;
        document.getElementById('cursor-time').textContent = formatTimestamp(currentTime);
        timeline.setAttribute('aria-valuenow', currentTime);
        timeline.setAttribute('aria-valuetext', formatTimestamp(currentTime));
        if (redraw) {
            drawTimeline();
            drawPlayfield();
        }
    }

    function seek(milliseconds) {
        updateCursor(milliseconds, { seekAudio: true });
    }

    async function loadScene(map) {
        const response = await post('/inpaint/preview-window/data', { key: map.key });
        if (response.key !== map.key) throw new Error('The preview map changed while loading.');
        scene = response.scene;
    }

    function loadAudio(map, targetTime) {
        if (currentAudioUrl === map.audio_url) {
            seek(targetTime);
            return;
        }
        currentAudioUrl = map.audio_url;
        audio.src = map.audio_url;
        audio.load();
        const applyTarget = () => seek(targetTime);
        if (audio.readyState >= 1) applyTarget();
        else audio.addEventListener('loadedmetadata', applyTarget, { once: true });
    }

    async function attemptPlay({ quiet = false } = {}) {
        if (!scene || !state?.map || state.map.mode !== 0) return;
        try {
            await audio.play();
            audioWasUsed = true;
            setStatus(state.generating ? 'Playing previous version' : 'Playing', state.generating ? 'busy' : 'ready');
        } catch (error) {
            if (!quiet) showToast('Playback needs a click on Play before automatic replay can begin.', true);
        }
    }

    function pauseAudio() {
        audio.pause();
        if (state?.has_session) setStatus(state.generating ? 'Generation in progress' : 'Ready', state.generating ? 'busy' : 'ready');
    }

    async function togglePlayback() {
        if (audio.paused) await attemptPlay();
        else pauseAudio();
    }

    async function renderState(nextState) {
        const hadMap = Boolean(state?.map);
        state = nextState;
        const hasMap = Boolean(state.has_session && state.map);
        document.getElementById('timeline-empty').hidden = hasMap;
        document.getElementById('playfield-empty').hidden = hasMap;

        if (!hasMap) {
            audio.pause();
            audio.removeAttribute('src');
            audio.load();
            scene = null;
            lastMapKey = null;
            lastMapIdentity = null;
            currentAudioUrl = null;
            danserRunning = false;
            document.getElementById('map-title').textContent = 'Preview';
            document.getElementById('map-subtitle').textContent = 'Open a beatmapset in the Inpaint tab.';
            document.getElementById('object-count').textContent = '— objects';
            setStatus('Waiting for map', 'waiting');
            setControlsEnabled(false);
            drawTimeline();
            drawPlayfield();
            return;
        }

        const map = state.map;
        const identity = `${state.selection.session_id}:${map.relative_path}`;
        const mapChanged = map.key !== lastMapKey;
        const identityChanged = identity !== lastMapIdentity;
        const wasPlaying = !audio.paused;
        let targetTime = currentTime;
        if (identityChanged || !hadMap) {
            targetTime = Math.max(0, state.selection.start_time - state.selection.padding_before);
        } else if (mapChanged && autoPlayUpdates.checked) {
            targetTime = Math.max(0, state.selection.start_time - state.selection.padding_before);
        } else {
            targetTime = Math.min(currentTime, Math.max(0, map.length_ms - 1));
        }

        document.getElementById('map-title').textContent = `${map.artist} — ${map.title}`;
        document.getElementById('map-subtitle').textContent = `${map.version} · mapped by ${map.mapper || 'Unknown'}`;
        document.getElementById('object-count').textContent = `${map.object_count.toLocaleString()} objects`;
        document.getElementById('map-length').textContent = formatTimestamp(map.length_ms);
        document.getElementById('selection-range').textContent =
            `${formatTimestamp(state.selection.start_time)}–${formatTimestamp(state.selection.end_time)}`;
        timeline.setAttribute('aria-valuemax', map.length_ms);

        if (map.mode !== 0) {
            scene = null;
            audio.pause();
            setStatus('Unsupported mode', 'error');
        } else if (mapChanged) {
            setStatus('Loading map…', 'busy');
            await loadScene(map);
            loadAudio(map, targetTime);
        } else {
            updateCursor(targetTime, { redraw: false });
        }

        if (state.generating) setStatus('Generation in progress', 'busy');
        else if (map.mode === 0 && audio.paused) setStatus('Ready', 'ready');
        else if (map.mode === 0) setStatus('Playing', 'ready');

        if (state.danser_available) {
            document.getElementById('danser-status').textContent =
                `Danser ${bootstrap.danserStatus?.version || '0.11.0'} is ready for a high-fidelity check.`;
        } else {
            document.getElementById('danser-status').textContent =
                `Optional: install Danser ${bootstrap.danserStatus?.version || '0.11.0'} for high-fidelity checks.`;
        }

        lastMapKey = map.key;
        lastMapIdentity = identity;
        setControlsEnabled(true);
        drawTimeline();
        drawPlayfield();

        const shouldPlayUpdate = mapChanged && !identityChanged && autoPlayUpdates.checked;
        if (wasPlaying || (shouldPlayUpdate && audioWasUsed)) await attemptPlay({ quiet: true });
    }

    function positionFromPointer(event) {
        const rect = timeline.getBoundingClientRect();
        const fraction = clamp((event.clientX - rect.left) / rect.width, 0, 1);
        return fraction * state.map.length_ms;
    }

    async function openInDanser() {
        if (!state?.map || danserLaunching || state.generating) return;
        danserLaunching = true;
        setControlsEnabled(true);
        document.getElementById('danser-status').textContent = `Opening ${formatTimestamp(currentTime)} in Danser…`;
        try {
            const response = await post('/inpaint/preview-window/play', { cursor: Math.round(currentTime) });
            danserRunning = true;
            document.getElementById('danser-status').textContent =
                `${response.viewer} is playing ${response.difficulty} from ${formatTimestamp(response.cursor)}.`;
        } catch (error) {
            danserRunning = false;
            document.getElementById('danser-status').textContent = error.message;
            showToast(error.message, true);
        } finally {
            danserLaunching = false;
            setControlsEnabled(Boolean(state?.map));
        }
    }

    async function stopDanser() {
        try {
            await post('/inpaint/preview-window/stop');
            danserRunning = false;
            document.getElementById('danser-status').textContent = 'Danser stopped. Embedded playback remains available.';
            setControlsEnabled(Boolean(state?.map));
        } catch (error) {
            showToast(error.message, true);
        }
    }

    async function copyBoundary(boundary) {
        if (!state?.map) return;
        try {
            const response = await post('/inpaint/preview-window/selection', {
                boundary,
                cursor: Math.round(currentTime)
            });
            state.selection = response.selection;
            document.getElementById('selection-range').textContent =
                `${formatTimestamp(state.selection.start_time)}–${formatTimestamp(state.selection.end_time)}`;
            drawTimeline();
            channel?.postMessage({
                type: 'selection',
                sessionId: response.selection.session_id,
                startTime: response.selection.start_time,
                endTime: response.selection.end_time
            });
            showToast(`Copied ${formatTimestamp(currentTime)} to ${boundary}.`);
        } catch (error) {
            showToast(error.message, true);
        }
    }

    async function pollState() {
        try {
            const response = await post('/inpaint/preview-window/state');
            await renderState(response);
        } catch (error) {
            setStatus('Disconnected', 'error');
            document.getElementById('danser-status').textContent = error.message;
        } finally {
            clearTimeout(pollTimer);
            pollTimer = setTimeout(pollState, 650);
        }
    }

    function animationFrame() {
        if (!audio.paused && state?.map) {
            updateCursor(audio.currentTime * 1_000, { redraw: false });
            drawTimeline();
        }
        drawPlayfield();
        window.requestAnimationFrame(animationFrame);
    }

    timeline.addEventListener('pointerdown', (event) => {
        if (!state?.map) return;
        timelineDragging = true;
        timeline.setPointerCapture(event.pointerId);
        seek(positionFromPointer(event));
    });
    timeline.addEventListener('pointermove', (event) => {
        if (timelineDragging && state?.map) seek(positionFromPointer(event));
    });
    timeline.addEventListener('pointerup', (event) => {
        if (!timelineDragging || !state?.map) return;
        timelineDragging = false;
        seek(positionFromPointer(event));
    });
    timeline.addEventListener('keydown', (event) => {
        if (!state?.map) return;
        const step = event.shiftKey ? 5_000 : 1_000;
        if (event.key === 'ArrowLeft') seek(currentTime - step);
        else if (event.key === 'ArrowRight') seek(currentTime + step);
        else if (event.key === 'Home') seek(0);
        else if (event.key === 'End') seek(state.map.length_ms - 1);
        else if (event.key === 'Enter') togglePlayback();
        else return;
        event.preventDefault();
    });

    audio.addEventListener('play', updatePlayButton);
    audio.addEventListener('pause', updatePlayButton);
    audio.addEventListener('ended', updatePlayButton);
    audio.addEventListener('error', () => {
        if (!currentAudioUrl) return;
        setStatus('Audio unavailable', 'error');
        showToast('The embedded player could not decode this map audio.', true);
    });

    document.getElementById('play-pause-button').addEventListener('click', togglePlayback);
    document.getElementById('jump-selection-button').addEventListener('click', () => {
        seek(Math.max(0, state.selection.start_time - state.selection.padding_before));
    });
    document.getElementById('danser-button').addEventListener('click', openInDanser);
    document.getElementById('stop-danser-button').addEventListener('click', stopDanser);
    document.getElementById('copy-start-button').addEventListener('click', () => copyBoundary('start'));
    document.getElementById('copy-end-button').addEventListener('click', () => copyBoundary('end'));
    autoPlayUpdates.addEventListener('change', () => {
        try { localStorage.setItem('inpaint.previewAutoPlayUpdates', autoPlayUpdates.checked ? 'true' : 'false'); } catch (_) {}
    });

    document.addEventListener('keydown', (event) => {
        const tag = event.target?.tagName?.toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'button') return;
        const key = event.key.toLowerCase();
        if (event.code === 'Space') togglePlayback();
        else if (event.key === '[') copyBoundary('start');
        else if (event.key === ']') copyBoundary('end');
        else if (key === 'r' && !event.ctrlKey && !event.metaKey) {
            channel?.postMessage({ type: 'regenerate', sessionId: state?.selection?.session_id });
            showToast('Regeneration requested in the Inpaint window.');
        } else if (key === 'z' && (event.ctrlKey || event.metaKey)) {
            channel?.postMessage({
                type: event.shiftKey ? 'redo' : 'undo',
                sessionId: state?.selection?.session_id
            });
        } else return;
        event.preventDefault();
    });

    window.addEventListener('resize', resizeCanvases);
    window.addEventListener('beforeunload', () => {
        fetch('/inpaint/preview-window/stop', {
            method: 'POST',
            headers: csrfHeaders,
            keepalive: true
        }).catch(() => {});
    });

    try {
        const saved = localStorage.getItem('inpaint.previewAutoPlayUpdates');
        if (saved !== null) autoPlayUpdates.checked = saved === 'true';
    } catch (_) {}
    updatePlayButton();
    resizeCanvases();
    window.requestAnimationFrame(animationFrame);
    pollState();
})();
