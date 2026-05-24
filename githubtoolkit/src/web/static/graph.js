// GitHub Social Graph Visualization
let cy = null;
let selectedUser = null;
let currentOffset = 0;
const PAGE_SIZE = 200;

const EDGE_COLORS = {
    follows:        '#0f3460',
    co_contributor: '#e9a100',
    forked:         '#00b894',
    starred:        '#a29bfe'
};

function initGraph() {
    cy = cytoscape({
        container: document.getElementById('cy'),
        style: [
            {
                selector: 'node',
                style: {
                    'background-color': '#e94560',
                    'label': 'data(name)',
                    'width': 'mapData(followers, 0, 10000, 20, 80)',
                    'height': 'mapData(followers, 0, 10000, 20, 80)',
                    'font-size': '10px',
                    'color': '#fff',
                    'text-outline-width': 2,
                    'text-outline-color': '#1a1a2e',
                    'text-valign': 'bottom',
                    'text-halign': 'center',
                    'text-margin-y': 5
                }
            },
            { selector: 'node:selected', style: { 'background-color': '#00d9ff', 'border-width': 3, 'border-color': '#fff' } },
            {
                selector: 'edge',
                style: {
                    'width': 1,
                    'line-color': 'data(color)',
                    'target-arrow-color': 'data(color)',
                    'target-arrow-shape': 'triangle',
                    'curve-style': 'bezier',
                    'opacity': 0.5
                }
            },
            { selector: 'edge:selected', style: { 'opacity': 1, 'width': 2 } }
        ],
        layout: { name: 'cose', animate: false, nodeRepulsion: 8000,
                  idealEdgeLength: 100, numIter: 1000 }
    });

    cy.on('tap', 'node', evt => {
        selectedUser = evt.target.data('id');
        showUserDetails(selectedUser);
        updateStats();
    });
    cy.on('tap', evt => {
        if (evt.target === cy) { selectedUser = null; hideUserDetails(); updateStats(); }
    });
    cy.on('dbltap', 'node', evt => {
        window.open(`https://github.com/${evt.target.data('id')}`, '_blank');
    });
}

function getVisibleEdgeTypes() {
    const types = [];
    document.querySelectorAll('.edge-toggle:checked').forEach(cb => types.push(cb.value));
    return new Set(types);
}

async function loadGraph(append = false) {
    if (!append) { currentOffset = 0; }
    showLoading(true);

    const minFollowers = parseInt(document.getElementById('filter-followers-min').value) || 0;
    const search = document.getElementById('filter-search').value.trim();
    const params = new URLSearchParams({
        limit: PAGE_SIZE, offset: currentOffset,
        min_followers: minFollowers,
        ...(search && { search })
    });

    try {
        const resp = await fetch(`/api/graph?${params}`);
        const data = await resp.json();
        const visibleTypes = getVisibleEdgeTypes();

        const elements = [];
        data.nodes.forEach(n => elements.push({
            data: { id: n.id, name: n.name, user_id: n.user_id,
                    avatar: n.avatar, followers: n.followers,
                    following: n.following, bio: n.bio }
        }));
        data.edges.forEach(e => {
            if (visibleTypes.has(e.type)) {
                elements.push({
                    data: { source: e.source, target: e.target,
                            type: e.type, color: EDGE_COLORS[e.type] || '#555' }
                });
            }
        });

        if (append) {
            cy.add(elements);
        } else {
            cy.elements().remove();
            cy.add(elements);
        }

        cy.layout({ name: 'cose', animate: false }).run();

        currentOffset += data.nodes.length;
        document.getElementById('btn-load-more').style.display =
            currentOffset < data.total ? 'block' : 'none';
        document.getElementById('graph-total').textContent =
            `Showing ${cy.nodes().length} / ${data.total} users`;

        updateStats();
        showLoading(false);
    } catch (err) {
        console.error('Graph load failed:', err);
        showLoading(false);
    }
}

function showLoading(show) {
    document.getElementById('loading').style.display = show ? 'block' : 'none';
}

function updateStats() {
    document.getElementById('stat-users').textContent = `Nodes: ${cy.nodes().length}`;
    document.getElementById('stat-edges').textContent = `Edges: ${cy.edges().length}`;
    document.getElementById('stat-selected').textContent =
        selectedUser ? `Selected: ${selectedUser}` : 'Selected: None';
}

async function showUserDetails(username) {
    try {
        const resp = await fetch(`/api/user/${username}`);
        const user = await resp.json();
        const container = document.getElementById('user-info');
        container.textContent = '';

        function addRow(label, value) {
            if (!value && value !== 0) return;
            const div = document.createElement('div');
            const strong = document.createElement('strong');
            strong.textContent = label + ': ';
            div.appendChild(strong);
            div.appendChild(document.createTextNode(String(value)));
            container.appendChild(div);
        }

        const img = document.createElement('img');
        img.src = user.avatar_url; img.alt = user.username;
        container.appendChild(img);

        addRow('Username', user.username);
        addRow('Name', user.display_name || 'N/A');
        addRow('Followers', user.followers_count);
        addRow('Following', user.following_count);
        addRow('Repos', user.public_repos);
        addRow('Email', user.email);
        addRow('Bio', user.bio);
        addRow('Location', user.location);
        addRow('Company', user.company);

        if (user.repos && user.repos.length > 0) {
            const hr = document.createElement('hr');
            container.appendChild(hr);
            const h3 = document.createElement('div');
            h3.style.color = '#e94560'; h3.style.fontWeight = 'bold';
            h3.textContent = 'Top Repos';
            container.appendChild(h3);
            user.repos.slice(0, 5).forEach(r => {
                const div = document.createElement('div');
                div.style.fontSize = '0.8em'; div.style.marginTop = '4px';
                div.textContent = `${r.full_name} ⭐${r.stars}${r.language ? ' · ' + r.language : ''}`;
                container.appendChild(div);
            });
        }

        document.getElementById('user-details').style.display = 'block';
    } catch (err) {
        console.error('Failed to load user details:', err);
    }
}

function hideUserDetails() {
    document.getElementById('user-details').style.display = 'none';
}

function applyFilters() { loadGraph(false); }

function resetFilters() {
    document.getElementById('filter-followers-min').value = '0';
    document.getElementById('filter-search').value = '';
    document.querySelectorAll('.edge-toggle').forEach(cb => cb.checked = true);
    loadGraph(false);
}

async function followUser() {
    if (!selectedUser) { alert('No user selected'); return; }
    if (!confirm(`Follow @${selectedUser}?`)) return;
    try {
        const resp = await fetch(`/api/follow/${selectedUser}`, { method: 'POST' });
        const result = await resp.json();
        alert(resp.ok ? `Followed @${selectedUser}` : `Failed: ${result.error}`);
    } catch (err) {
        alert('Failed to follow user');
    }
}

// Event listeners
document.getElementById('btn-apply-filters').addEventListener('click', applyFilters);
document.getElementById('btn-reset-filters').addEventListener('click', resetFilters);
document.getElementById('btn-refresh').addEventListener('click', () => loadGraph(false));
document.getElementById('btn-fit').addEventListener('click', () => cy.fit());
document.getElementById('btn-load-more').addEventListener('click', () => loadGraph(true));
document.getElementById('btn-open-github').addEventListener('click', () => {
    if (selectedUser) window.open(`https://github.com/${selectedUser}`, '_blank');
});
document.getElementById('btn-follow').addEventListener('click', followUser);
document.querySelectorAll('.edge-toggle').forEach(cb =>
    cb.addEventListener('change', () => loadGraph(false)));

document.addEventListener('DOMContentLoaded', () => { initGraph(); loadGraph(false); });
