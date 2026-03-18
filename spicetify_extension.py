SPICETIFY_EXTENSION_JS = r"""
(async function() {
    while (!Spicetify?.Player?.seek || !Spicetify?.CosmosAsync || !Spicetify?.Platform?.PlayerAPI) {
        await new Promise(r => setTimeout(r, 100));
    }
    console.log("music-overlay: Spicetify extension loaded");

    const pendingSeek = sessionStorage.getItem('spicetify-seek');
    if (pendingSeek) {
        sessionStorage.removeItem('spicetify-seek');
        const pos = parseFloat(pendingSeek);
        await new Promise(r => {
            const check = setInterval(() => {
                if (Spicetify.Player.getProgress() > 0 || pos === 0) {
                    clearInterval(check);
                    Spicetify.Player.seek(pos * 1000);
                    console.log("music-overlay: post-play seek to", pos);
                    r();
                }
            }, 200);
        });
    }

    let ws = null;

    function report(msg) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ log: msg }));
        }
    }

    async function searchAndPlay(title, artist, seek_after) {
        report(`searching for "${title}" by "${artist}"`);
        try {
            let track = null;

            // Primary: searchModalResults
            try {
                const result = await Spicetify.GraphQL.Request(
                    Spicetify.GraphQL.Definitions.searchModalResults,
                    {
                        searchTerm: `${title} ${artist}`,
                        offset: 0,
                        limit: 5,
                        numberOfTopResults: 5,
                        includeAudiobooks: false,
                        includeArtists: false,
                        includeAlbums: false,
                        includePlaylists: false,
                        includeProfiles: false,
                        includeGenres: false,
                        includeEpisodes: false,
                        includePodcasts: false,
                        includeAuthors: false,
                    }
                );
                track =
                    result?.data?.searchV2?.tracksV2?.items?.[0]?.item?.data ||
                    result?.data?.searchV2?.tracks?.items?.[0]?.item?.data ||
                    result?.data?.searchV2?.topResultsV2?.itemsV2?.[0]?.item?.data;
            } catch(e) {
                report(`searchModalResults error: ${e?.message || String(e)}`);
            }

            // Fallback: searchSuggestions
            if (!track?.uri) {
                try {
                    const result = await Spicetify.GraphQL.Request(
                        Spicetify.GraphQL.Definitions.searchSuggestions,
                        { query: `${title} ${artist}` }
                    );
                    track =
                        result?.data?.searchSuggestions?.tracks?.items?.[0] ||
                        result?.data?.suggestions?.tracks?.[0];
                } catch(e) {
                    report(`searchSuggestions error: ${e?.message || String(e)}`);
                }
            }

            if (track?.uri) {
                report(`playing: ${track.name} — ${track.uri}`);
                if (seek_after !== undefined && seek_after > 0) {
                    sessionStorage.setItem('spicetify-seek', String(seek_after));
                }
                await Spicetify.Player.playUri(track.uri);
            } else {
                report(`no track found for: ${title} ${artist}`);
            }
        } catch(e) {
            report(`search error: ${e?.message || String(e)}`);
        }
    }

    function connect() {
        ws = new WebSocket("ws://localhost:9001");

        ws.onopen = () => {
            console.log("music-overlay: connected");
            report("extension connected and ready");
        };

        ws.onmessage = async (event) => {
            try {
                const data = JSON.parse(event.data);

                if (data.seek !== undefined) {
                    Spicetify.Player.seek(data.seek * 1000);
                    report(`seeked to ${data.seek}`);
                }

                if (data.toggle_play) {
                    Spicetify.Player.togglePlay();
                    report("toggle play");
                }

                if (data.play_song !== undefined) {
                    const { title, artist, seek_after } = data.play_song;
                    await searchAndPlay(title, artist, seek_after);
                }

            } catch(e) {
                report(`parse error: ${e.message}`);
                console.error("music-overlay: error parsing message", e);
            }
        };

        ws.onclose = () => {
            console.log("music-overlay: disconnected, retrying in 3s");
            setTimeout(connect, 3000);
        };

        ws.onerror = (e) => {
            console.error("music-overlay: ws error", e);
            ws.close();
        };
    }

    connect();
})();
"""