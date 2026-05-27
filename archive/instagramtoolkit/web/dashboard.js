// Dashboard JS — fetches data from local API endpoints (127.0.0.1 only).
// Uses Cytoscape.js for graph rendering; no D3.js, no JSON flat-file reads.

const API = {
    users:       '/api/users',
    graph:       '/api/graph?limit=500&offset=0',
    stats:       '/api/stats',
};

// ── Colour pool per scraping account ─────────────────────────────────────────
const ACCOUNT_COLOURS = [
    '#4e79a7', '#f28e2b', '#e15759', '#76b7b2',
    '#59a14f', '#edc948', '#b07aa1', '#ff9da7',
];
const accountColour = (() => {
    const map = {};
    let idx = 0;
    return (account) => {
        if (!map[account]) map[account] = ACCOUNT_COLOURS[idx++ % ACCOUNT_COLOURS.length];
        return map[account];
    };
})();

// ── Node size from follower count ─────────────────────────────────────────────
function nodeSize(followers) {
    const s = Math.sqrt(followers || 1) * 5;
    return Math.max(10, Math.min(50, s));
}

// ── Load and render summary table ─────────────────────────────────────────────
async function loadSummary() {
    try {
        const resp = await fetch(API.users);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const users = Object.values(data).sort((a, b) => (b.followers_count || 0) - (a.followers_count || 0));

        const tbody = document.querySelector('#summaryTable tbody');
        tbody.innerHTML = '';
        users.forEach(u => {
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${u.username}</td><td>${u.followers_count || 0}</td><td>${u.following_count || 0}</td><td>${u.is_public ? '✓' : '✗'}</td>`;
            tbody.appendChild(tr);
        });
    } catch (e) {
        console.warn('Summary load failed:', e);
    }
}

// ── Load and render stats badge ───────────────────────────────────────────────
async function loadStats() {
    try {
        const resp = await fetch(API.stats);
        if (!resp.ok) return;
        const s = await resp.json();
        document.getElementById('stats-badge').textContent =
            `(${s.profiles} profiles · ${s.relationships} edges · ${s.usernames} tracked)`;
    } catch (e) { /* silent */ }
}

// ── Load graph data and render with Cytoscape.js ──────────────────────────────
async function loadGraph() {
    const status = document.getElementById('graph-status');
    status.textContent = 'Loading graph…';

    let graphData;
    try {
        const resp = await fetch(API.graph);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        graphData = await resp.json();
    } catch (e) {
        status.textContent = `Graph load failed: ${e.message}`;
        return;
    }

    const { nodes, edges } = graphData;
    if (!nodes || nodes.length === 0) {
        status.textContent = 'No graph data yet. Run the spider first.';
        return;
    }

    // Build Cytoscape elements
    const elements = [
        ...nodes.map(n => ({
            data: {
                id: n.id,
                label: n.id,
                followers: n.followers || 0,
                is_public: n.is_public,
                size: nodeSize(n.followers),
            }
        })),
        ...edges.map(e => ({
            data: {
                source: e.source,
                target: e.target,
                collected_by: e.collected_by || '',
            }
        })),
    ];

    const cy = cytoscape({
        container: document.getElementById('cy'),
        elements,
        style: [
            {
                selector: 'node',
                style: {
                    'label': 'data(label)',
                    'width': 'data(size)',
                    'height': 'data(size)',
                    'font-size': '9px',
                    'text-valign': 'bottom',
                    'text-margin-y': '4px',
                    'background-color': '#4e79a7',
                    'border-width': 1,
                    'border-color': '#fff',
                }
            },
            {
                selector: 'edge',
                style: {
                    'width': 1,
                    'line-color': '#ccc',
                    'target-arrow-color': '#ccc',
                    'target-arrow-shape': 'triangle',
                    'curve-style': 'bezier',
                    'opacity': 0.6,
                }
            },
        ],
        layout: {
            name: 'cose',
            animate: false,
            idealEdgeLength: 80,
            nodeRepulsion: 400000,
        }
    });

    // Colour nodes by scraping account (edge collected_by)
    const nodeAccount = {};
    edges.forEach(e => {
        if (e.collected_by) nodeAccount[e.source] = e.collected_by;
    });
    cy.nodes().forEach(n => {
        const acct = nodeAccount[n.id()];
        if (acct) n.style('background-color', accountColour(acct));
    });

    status.textContent = `Rendered ${nodes.length} nodes · ${edges.length} edges`;
}

// ── Init ──────────────────────────────────────────────────────────────────────
(async () => {
    await Promise.all([loadSummary(), loadStats(), loadGraph()]);
})();
