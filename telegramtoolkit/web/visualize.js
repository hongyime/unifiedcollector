// Network visualization variables
let users = {}, memberships = [], loadedGroups = new Set();
let allNodes = new Map(), allEdges = new Map();
const nodes = new vis.DataSet();
const edges = new vis.DataSet();
let network;
let currentView = 'usergroup';
let isLoading = false;
let performanceMode = false;

// Performance limits
const MAX_NODES = 1500;
const MAX_EDGES = 3000;
const CHUNK_SIZE = 1000;

const accountColors = {
  'bryanseah234': '#45B7D1',
  'oopspwned': '#96CEB4',
  'shotsbyseah234': '#FFEAA7',
  'unknown': '#95A5A6'
};

// Enhanced error handling
function handleError(context, error, fallback) {
  console.error(`Error in ${context}:`, error);

  const errorDiv = document.createElement('div');
  errorDiv.style.cssText = `
    position: fixed; top: 20px; right: 20px; 
    background: #ffebee; color: #c62828; 
    padding: 15px; border-radius: 5px; 
    border-left: 4px solid #f44336; z-index: 10000;
    max-width: 400px; box-shadow: 0 4px 8px rgba(0,0,0,0.3);
  `;
  errorDiv.innerHTML = `
    <strong>Error: ${context}</strong><br>
    ${error.message || error}<br>
    <button onclick="this.parentElement.remove()" style="margin-top: 8px; padding: 5px 12px; background: #c62828; color: white; border: none; border-radius: 3px; cursor: pointer;">Dismiss</button>
  `;
  document.body.appendChild(errorDiv);

  setTimeout(() => errorDiv.remove(), 10000);

  if (fallback) setTimeout(fallback, 1000);
}

// Progress tracking
function updateLoadingProgress(stage, progress) {
  const loadingPanel = document.getElementById('loadingPanel');
  if (loadingPanel) {
    loadingPanel.innerHTML = `
      <h3>📊 Loading Network Data...</h3>
      <p>${stage}</p>
      <div style="background: #e0e0e0; height: 6px; border-radius: 3px; margin-top: 10px;">
        <div style="background: #667eea; height: 100%; width: ${progress}%; border-radius: 3px; transition: width 0.3s;"></div>
      </div>
    `;
  }
}

// Memory-efficient data processing
function processDataInChunks(data, processor, chunkSize = CHUNK_SIZE) {
  return new Promise((resolve) => {
    let index = 0;
    const results = [];

    function processChunk() {
      const chunk = data.slice(index, index + chunkSize);
      chunk.forEach(item => {
        try {
          const result = processor(item);
          if (result) results.push(result);
        } catch (error) {
          console.warn('Error processing item:', error);
        }
      });

      index += chunkSize;
      const progress = Math.min(100, (index / data.length) * 100);
      updateLoadingProgress(`Processing ${Math.min(index, data.length)}/${data.length} items`, progress);

      if (index < data.length) {
        setTimeout(processChunk, 10); // Small delay to prevent UI blocking
      } else {
        resolve(results);
      }
    }

    processChunk();
  });
}

// Load data with enhanced error handling
async function loadUsers() {
  isLoading = true;
  updateLoadingProgress('Loading users data...', 0);
  try {
    const indexResponse = await fetch('/data/visualize_index.json');
    if (indexResponse.ok) {
      const indexData = await indexResponse.json();
      users = indexData.users || {};
      memberships = Array.isArray(indexData.memberships) ? indexData.memberships : [];
      if (Object.keys(users).length > MAX_NODES / 2) {
        performanceMode = true;
      }
      if (memberships.length > MAX_EDGES) {
        showPerformanceWarning();
      }
      updateLoadingProgress('Initializing network...', 95);
      document.getElementById('loadingPanel').style.display = 'none';
      isLoading = false;
      initializeNetwork();
      renderUserGroupGraph();
      console.log('✅ Loaded compact visualization index');
      return;
    }
  } catch (error) {
    console.log('Compact visualization index not available, falling back to CSV');
  }

  // Hardcoded to only use /data/Users.csv for reliability
  const path = '/data/Users.csv';
  try {
    console.log(`Trying users path: ${path}`);
    await new Promise((resolve, reject) => {
      Papa.parse(path, {
        header: true,
        download: true,
        complete: function (results) {
          console.log(`✅ Loaded users from ${path}`);
          processUsers(results);
          resolve();
        },
        error: reject
      });
    });
    // If we reach here, the file was loaded successfully
    return;
  } catch (error) {
    console.warn(`Failed to load users from ${path}:`, error.message);
    handleError('loading users from /data/Users.csv', error, () => {
      document.getElementById('loadingPanel').innerHTML = `
        <h3>❌ No User Data</h3>
        <p>Could not load Users.csv file from <b>/data/Users.csv</b></p>
        <div style="margin: 15px 0; font-size: 0.9rem; text-align: left;">
          <strong>Tried path:</strong> /data/Users.csv
        </div>
        <div style="margin: 15px 0; font-size: 0.9rem;">
          <strong>Solution:</strong><br>
          1. Run: <code>python -m toolkit.server.simple_server</code><br>
          2. Open: <code>http://localhost:8000/web/visualize.html</code>
        </div>
        <button onclick="location.reload()" style="padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 5px; cursor: pointer; margin-top: 15px;">Retry</button>
      `;
    });
  }
}

async function processUsers(results) {
  updateLoadingProgress('Processing users...', 25);

  try {
    await processDataInChunks(results.data, (row) => {
      if (row.user_id && row.user_id.trim()) {
        users[row.user_id] = {
          username: (row.username || "").trim(),
          first_name: (row.first_name || "").trim(),
          last_name: (row.last_name || "").trim(),
          is_bot: row.is_bot === 'True',
          display_name: row.username || row.first_name || `User ${row.user_id}`
        };
        return true;
      }
      return false;
    });

    console.log(`Loaded ${Object.keys(users).length} users`);

    // Check for performance mode
    if (Object.keys(users).length > MAX_NODES / 2) {
      performanceMode = true;
      console.warn('Large dataset detected, enabling performance mode');
    }

    loadMemberships();
  } catch (error) {
    handleError('processing users', error);
  }
}

async function loadMemberships() {
  updateLoadingProgress('Loading memberships data...', 50);

  // Hardcoded to only use /data/Memberships.csv for reliability
  const path = '/data/Memberships.csv';
  try {
    console.log(`Trying memberships path: ${path}`);
    await new Promise((resolve, reject) => {
      Papa.parse(path, {
        header: true,
        download: true,
        complete: function (results) {
          console.log(`✅ Loaded memberships from ${path}`);
          processMemberships(results);
          resolve();
        },
        error: reject
      });
    });
    // If we reach here, the file was loaded successfully
    return;
  } catch (error) {
    console.warn(`Failed to load memberships from ${path}:`, error.message);
    handleError('loading memberships from /data/Memberships.csv', error, () => {
      document.getElementById('loadingPanel').innerHTML = `
        <h3>❌ No Membership Data</h3>
        <p>Could not load Memberships.csv file from <b>/data/Memberships.csv</b></p>
        <div style="margin: 15px 0; font-size: 0.9rem; text-align: left;">
          <strong>Tried path:</strong> /data/Memberships.csv
        </div>
        <div style="margin: 15px 0; font-size: 0.9rem;">
          <strong>Solution:</strong><br>
          1. Run: <code>python -m toolkit.server.simple_server</code><br>
          2. Open: <code>http://localhost:8000/web/visualize.html</code>
        </div>
        <button onclick="location.reload()" style="padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 5px; cursor: pointer; margin-top: 15px;">Retry</button>
      `;
    });
  }
}

async function processMemberships(results) {
  updateLoadingProgress('Processing memberships...', 75);

  try {
    const validMemberships = [];
    await processDataInChunks(results.data, (row) => {
      if (row.user_id && row.group_name && row.group_name.trim()) {
        validMemberships.push({
          user_id: row.user_id,
          group_name: row.group_name.trim(),
          group_id: row.group_id || 'unknown',
          username: row.username || ''
        });
        return true;
      }
      return false;
    });

    memberships = validMemberships;
    console.log(`Loaded ${memberships.length} memberships`);

    // Performance warning
    if (memberships.length > MAX_EDGES) {
      showPerformanceWarning();
    }

    updateLoadingProgress('Initializing network...', 95);
    document.getElementById('loadingPanel').style.display = 'none';
    isLoading = false;

    initializeNetwork();
    renderUserGroupGraph();
  } catch (error) {
    handleError('processing memberships', error);
  }
}

function showPerformanceWarning() {
  const warning = document.createElement('div');
  warning.style.cssText = `
    position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
    background: #fff3cd; color: #856404; padding: 15px; border-radius: 5px;
    border-left: 4px solid #ffc107; z-index: 10000; max-width: 500px;
    box-shadow: 0 4px 8px rgba(0,0,0,0.3); text-align: center;
  `;
  warning.innerHTML = `
    <strong>Large Dataset Warning</strong><br>
    ${memberships.length.toLocaleString()} connections detected<br>
    <small>Visualization may be slower. Consider filtering data for better performance.</small><br>
    <button onclick="this.parentElement.remove()" style="margin-top: 10px; padding: 5px 15px; background: #856404; color: white; border: none; border-radius: 3px; cursor: pointer;">Continue</button>
  `;
  document.body.appendChild(warning);
}

function initializeNetwork() {
  const container = document.getElementById('network');
  const data = { nodes: nodes, edges: edges };

  const options = {
    physics: {
      enabled: true,
      stabilization: { iterations: 100 },
      barnesHut: {
        gravitationalConstant: -2000,
        centralGravity: 0.3,
        springLength: 95,
        springConstant: 0.04,
        damping: 0.09
      }
    },
    nodes: {
      borderWidth: 2,
      shadow: true,
      font: { size: 12, color: '#333' }
    },
    edges: {
      width: 1,
      color: { color: '#848484', opacity: 0.6 },
      smooth: { type: 'continuous' }
    },
    interaction: {
      hover: true,
      selectConnectedEdges: false
    }
  };

  network = new vis.Network(container, data, options);

  // Handle node clicks
  network.on("click", function (params) {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      expandNode(nodeId);
    }
  });

  // Update stats on stabilization
  network.on("stabilizationIterationsDone", function () {
    updateStats();
  });
}

function renderUserGroupGraph() {
  currentView = 'usergroup';
  setActiveButton('userGroupBtn');

  nodes.clear();
  edges.clear();
  allNodes.clear();
  allEdges.clear();
  loadedGroups.clear();

  // Build a map of user_id to all groups they are in
  const userGroupsMap = {};
  memberships.forEach(m => {
    if (!userGroupsMap[m.user_id]) userGroupsMap[m.user_id] = new Set();
    userGroupsMap[m.user_id].add(m.group_name);
  });

  // Only keep users who are in multiple groups
  const multiGroupUsers = new Set(Object.entries(userGroupsMap)
    .filter(([userId, groupSet]) => groupSet.size > 1)
    .map(([userId]) => userId));

  // Rebuild group counts, but only counting multi-group users
  const groupCounts = {};
  memberships.forEach(m => {
    if (multiGroupUsers.has(m.user_id)) {
      const key = `${m.group_name}|||${m.account || 'unknown'}`;
      groupCounts[key] = (groupCounts[key] || 0) + 1;
    }
  });

  const topGroups = Object.entries(groupCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)
    .map(([key, count]) => {
      const [groupName, account] = key.split('|||');
      return { groupName, account, count };
    });

  // Add group nodes
  topGroups.forEach(({ groupName, account, count }) => {
    const groupId = `group_${groupName}_${account}`;
    const color = accountColors[account] || accountColors.unknown;
    allNodes.set(groupId, {
      id: groupId,
      label: `${groupName}\n(${count} users)`,
      color: { background: '#FF6B6B', border: color },
      shape: 'box',
      size: Math.min(30 + count * 2, 80),
      group: 'group',
      account: account,
      groupName: groupName,
      memberCount: count
    });
  });

  // Add user nodes and edges for users in multiple groups
  // For each group, add only multi-group users
  topGroups.forEach(({ groupName, account }) => {
    const groupId = `group_${groupName}_${account}`;
    memberships.forEach(m => {
      if (m.group_name === groupName && (m.account || 'unknown') === account && multiGroupUsers.has(m.user_id)) {
        const userId = `user_${m.user_id}`;
        if (!allNodes.has(userId)) {
          const user = users[m.user_id] || {};
          allNodes.set(userId, {
            id: userId,
            label: user.display_name || `User ${m.user_id}`,
            color: { background: '#4ECDC4', border: '#333' },
            shape: 'dot',
            size: 15,
            group: 'user',
            userId: m.user_id
          });
        }
        const edgeId = `${groupId}_${userId}`;
        if (!allEdges.has(edgeId)) {
          allEdges.set(edgeId, {
            id: edgeId,
            from: groupId,
            to: userId,
            color: { color: '#999', opacity: 0.5 }
          });
        }
      }
    });
  });

  nodes.add(Array.from(allNodes.values()));
  edges.add(Array.from(allEdges.values()));
  updateStats();
}

function renderGroupSimilarityGraph() {
  currentView = 'similarity';
  setActiveButton('groupSimilarityBtn');

  nodes.clear();
  edges.clear();
  allNodes.clear();
  allEdges.clear();

  // Calculate group-to-group similarity based on shared users
  const groupUsers = {};
  memberships.forEach(m => {
    const key = `${m.group_name}|||${m.account || 'unknown'}`;
    if (!groupUsers[key]) groupUsers[key] = new Set();
    groupUsers[key].add(m.user_id);
  });

  const groups = Object.keys(groupUsers);
  const similarities = [];

  // Calculate Jaccard similarity between groups
  for (let i = 0; i < groups.length; i++) {
    for (let j = i + 1; j < groups.length; j++) {
      const users1 = groupUsers[groups[i]];
      const users2 = groupUsers[groups[j]];
      const intersection = new Set([...users1].filter(x => users2.has(x)));
      const union = new Set([...users1, ...users2]);
      const similarity = intersection.size / union.size;

      if (similarity > 0.05) { // Only show significant similarities
        similarities.push({
          group1: groups[i],
          group2: groups[j],
          similarity: similarity,
          sharedUsers: intersection.size
        });
      }
    }
  }

  // Add group nodes
  const significantGroups = new Set();
  similarities.forEach(sim => {
    significantGroups.add(sim.group1);
    significantGroups.add(sim.group2);
  });

  significantGroups.forEach(groupKey => {
    const [groupName, account] = groupKey.split('|||');
    const userCount = groupUsers[groupKey].size;
    const color = accountColors[account] || accountColors.unknown;

    allNodes.set(groupKey, {
      id: groupKey,
      label: `${groupName}\n(${userCount} users)`,
      color: { background: color, border: '#333' },
      shape: 'ellipse',
      size: Math.min(20 + userCount * 1.5, 60),
      group: 'group',
      account: account
    });
  });

  // Add similarity edges
  similarities.forEach(sim => {
    const edgeId = `${sim.group1}_${sim.group2}`;
    allEdges.set(edgeId, {
      id: edgeId,
      from: sim.group1,
      to: sim.group2,
      width: Math.max(1, sim.similarity * 10),
      label: `${sim.sharedUsers} shared`,
      color: { color: '#666', opacity: 0.7 }
    });
  });

  nodes.add(Array.from(allNodes.values()));
  edges.add(Array.from(allEdges.values()));
  updateStats();
}

function expandNode(nodeId) {
  if (currentView !== 'usergroup') return;

  if (loadedGroups.has(nodeId)) return; // Already expanded

  const node = allNodes.get(nodeId);
  if (!node || node.group !== 'group') return;

  loadedGroups.add(nodeId);

  // Find all users in this group
  const groupUsers = memberships.filter(m =>
    m.group_name === node.groupName &&
    (m.account || 'unknown') === node.account
  );

  // Add user nodes (limit to 50 to avoid overcrowding)
  const limitedUsers = groupUsers.slice(0, 10);
  const newNodes = [];
  const newEdges = [];

  limitedUsers.forEach(membership => {
    const userId = `user_${membership.user_id}`;
    const user = users[membership.user_id] || {};

    if (!allNodes.has(userId)) {
      const userNode = {
        id: userId,
        label: user.display_name || `User ${membership.user_id}`,
        color: { background: '#4ECDC4', border: '#333' },
        shape: 'dot',
        size: 15,
        group: 'user',
        userId: membership.user_id
      };

      allNodes.set(userId, userNode);
      newNodes.push(userNode);
    }

    // Add edge between group and user
    const edgeId = `${nodeId}_${userId}`;
    if (!allEdges.has(edgeId)) {
      const edge = {
        id: edgeId,
        from: nodeId,
        to: userId,
        color: { color: '#999', opacity: 0.5 }
      };

      allEdges.set(edgeId, edge);
      newEdges.push(edge);
    }
  });

  nodes.add(newNodes);
  edges.add(newEdges);
  updateStats();
}

function setActiveButton(activeId) {
  ['userGroupBtn', 'groupSimilarityBtn'].forEach(id => {
    document.getElementById(id).classList.remove('active');
  });
  document.getElementById(activeId).classList.add('active');
}

function resetView() {
  if (currentView === 'usergroup') {
    renderUserGroupGraph();
  } else {
    renderGroupSimilarityGraph();
  }
}

function fitNetwork() {
  if (network) {
    network.fit();
  }
}

function updateStats() {
  document.getElementById('nodeCount').textContent = allNodes.size;
  document.getElementById('edgeCount').textContent = allEdges.size;
  document.getElementById('visibleCount').textContent = `${nodes.length} nodes, ${edges.length} edges`;
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
  // Load data
  loadUsers();
});
