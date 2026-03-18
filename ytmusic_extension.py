MANIFEST_JSON = r"""
{
  "manifest_version": 3,
  "name": "music-overlay",
  "version": "1.0",
  "description": "Sync YT Music with music-overlay jam sessions",
  "permissions": ["activeTab", "scripting", "tabs"],
  "host_permissions": [
    "*://music.youtube.com/*"
  ],
  "content_scripts": [
    {
      "matches": ["*://music.youtube.com/*"],
      "js": ["content.js"],
      "world": "MAIN",
      "run_at": "document_start"
    }
  ]
}
"""

CONTENT_JS = r"""
const originalAddEventListener = window.addEventListener;
window.addEventListener = function(type, listener, options) {
    if (type === 'beforeunload') {
        console.log("music-overlay: Successfully blocked YTM beforeunload listener.");
        return; 
    }
    return originalAddEventListener.call(this, type, listener, options);
};

Object.defineProperty(window, 'onbeforeunload', {
    get: function() { return null; },
    set: function() { console.log("music-overlay: Successfully blocked YTM onbeforeunload property."); }
});

document.addEventListener('DOMContentLoaded', async () => {
    if (window.location.hostname !== "music.youtube.com") return;

    function getVideo() {
        return document.querySelector('video');
    }

    // wait for video element to exist
    while (!getVideo()) {
        await new Promise(r => setTimeout(r, 500));
    }

    const pendingSeek = sessionStorage.getItem('music-overlay-seek');
    if (pendingSeek) {
        sessionStorage.removeItem('music-overlay-seek');
        const pos = parseFloat(pendingSeek);
        
        await new Promise(r => {
            const check = setInterval(() => {
                const v = getVideo();
                if (v && v.readyState >= 2) {
                    clearInterval(check);
                    v.currentTime = pos;
                    
                    if (v.paused) v.play().catch(err => console.log("Autoplay blocked", err));
                    
                    console.log("music-overlay: post-navigate seek to", pos);
                    r();
                }
            }, 200);
        });
    }

    console.log("music-overlay: YT Music extension loaded (God Mode)");

    // websocket
    function connect() {
        const ws = new WebSocket("ws://localhost:9002");

        ws.onopen = () => {
            console.log("music-overlay: WebSocket connected");
            document.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const video = getVideo();

                if (data.play_video !== undefined) {
                    if (data.seek_after !== undefined) {
                        sessionStorage.setItem('music-overlay-seek', String(data.seek_after));
                    }
                    
                    window.location.href = `/watch?v=${data.play_video}`;
                    return;
                }

                if (!video) return;

                if (data.seek !== undefined) {
                    video.currentTime = data.seek;
                    console.log("music-overlay: seeked to", data.seek);
                }

                if (data.toggle_play) {
                    if (video.paused) video.play();
                    else video.pause();
                }
            } catch(e) {
                console.error("music-overlay: error", e);
            }
        };

        ws.onclose = () => {
            setTimeout(connect, 3000);
        };

        ws.onerror = () => ws.close();
    }

    connect();
});
"""