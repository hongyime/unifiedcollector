document.addEventListener('DOMContentLoaded', () => {
  let allUsers = [], memberships = [];
  let userMembershipsIndex = new Map();
  let filteredUsers = []; // Cache filtered results
  let currentPage = 1, usersPerPage = 60;
  let currentLetter = 'A';
  let groupCounts = new Map();
  let isLoading = false;
  let loadingProgress = { users: 0, memberships: 0 };

  // Performance monitoring
  let performanceMetrics = {
    totalRecords: 0,
    loadTime: 0,
    renderTime: 0
  };

  const userGrid = document.getElementById('userGrid');
  const groupList = document.getElementById('groupList');
  const prevBtn = document.getElementById('prevPage');
  const nextBtn = document.getElementById('nextPage');
  const pageIndicator = document.getElementById('pageIndicator');
  const letterFilter = document.getElementById('letterFilter');
  const loadingIndicator = document.getElementById('loadingIndicator');

  // Stats elements
  const totalUsersEl = document.getElementById('totalUsers');
  const totalGroupsEl = document.getElementById('totalGroups');
  const totalMembershipsEl = document.getElementById('totalMemberships');

  // Enhanced error handling and recovery
  function handleError(context, error, fallbackAction) {
    console.error(`Error in ${context}:`, error);

    // Show user-friendly error message
    const errorDiv = document.createElement('div');
    errorDiv.className = 'error-message';
    errorDiv.style.cssText = `
      background: #ffebee; 
      color: #c62828; 
      padding: 15px; 
      margin: 10px; 
      border-radius: 5px; 
      border-left: 4px solid #f44336;
    `;
    errorDiv.innerHTML = `
      <strong>Error loading ${context}:</strong><br>
      ${error.message || error}<br>
      <small>Attempting recovery...</small>
    `;
    document.body.insertBefore(errorDiv, document.body.firstChild);

    // Auto-remove error message after 5 seconds
    setTimeout(() => errorDiv.remove(), 5000);

    // Execute fallback action if provided
    if (fallbackAction) {
      setTimeout(fallbackAction, 1000);
    }
  }

  // Progress tracking for large datasets
  function updateProgress(type, current, total) {
    loadingProgress[type] = (current / total) * 100;
    const totalProgress = (loadingProgress.users + loadingProgress.memberships) / 2;

    if (loadingIndicator) {
      loadingIndicator.innerHTML = `
        <div>Loading data... ${Math.round(totalProgress)}%</div>
        <div style="background: #e0e0e0; height: 4px; border-radius: 2px; margin-top: 5px;">
          <div style="background: #667eea; height: 100%; width: ${totalProgress}%; border-radius: 2px; transition: width 0.3s;"></div>
        </div>
      `;
    }
  }

  // Memory-efficient data filtering with indexing
  function createUserIndex() {
    const startTime = performance.now();
    const userIndex = new Map();

    allUsers.forEach((user, index) => {
      const name = (user.username || user.first_name || user.user_id || '').trim();
      if (name) {
        const firstChar = name[0].toUpperCase();
        const key = /[A-Z]/.test(firstChar) ? firstChar : '#';

        if (!userIndex.has(key)) {
          userIndex.set(key, []);
        }
        userIndex.get(key).push(index);
      }
    });

    performanceMetrics.renderTime = performance.now() - startTime;
    console.log(`User index created in ${performanceMetrics.renderTime.toFixed(2)}ms`);
    return userIndex;
  }

  let userIndex = new Map();

  // Debounced filtering for better performance
  let filterTimeout;
  function debouncedFilter(letter) {
    clearTimeout(filterTimeout);
    filterTimeout = setTimeout(() => {
      currentLetter = letter;
      currentPage = 1;
      setActiveLetterBtn(letter);
      renderUsers();
    }, 100);
  }

  // Render A-Z + # filter buttons
  const letters = [...'ABCDEFGHIJKLMNOPQRSTUVWXYZ', '#'];
  letterFilter.innerHTML = letters.map(l =>
    `<button data-letter="${l}" class="letter-btn">${l}</button>`
  ).join(' ');

  // Set initial active letter button
  setActiveLetterBtn(currentLetter);

  letterFilter.addEventListener('click', e => {
    if (!e.target.matches('button') || isLoading) return;
    debouncedFilter(e.target.dataset.letter);
  });

  function setActiveLetterBtn(letter) {
    [...letterFilter.querySelectorAll('button')].forEach(btn =>
      btn.classList.toggle('active', btn.dataset.letter === letter)
    );
  }

  function applyDashboardIndex(indexData) {
    allUsers = Array.isArray(indexData.users) ? indexData.users : [];
    userMembershipsIndex = new Map();
    const rawMemberships = indexData.user_memberships || {};
    Object.entries(rawMemberships).forEach(([userId, list]) => {
      if (Array.isArray(list)) {
        userMembershipsIndex.set(String(userId), list);
      }
    });
    memberships = [];
    groupCounts.clear();
    const rawGroupCounts = indexData.group_counts || {};
    Object.entries(rawGroupCounts).forEach(([key, count]) => {
      const parts = key.split('|||');
      const label = `${parts[0] || 'Unknown'} (${parts[1] || 'unknown'})`;
      groupCounts.set(label, Number(count) || 0);
    });
    totalUsersEl.textContent = allUsers.length.toLocaleString();
    totalGroupsEl.textContent = groupCounts.size.toLocaleString();
    const totalMemberships = Array.from(groupCounts.values()).reduce((sum, value) => sum + value, 0);
    totalMembershipsEl.textContent = totalMemberships.toLocaleString();
    performanceMetrics.totalRecords = allUsers.length + totalMemberships;
    userIndex = createUserIndex();
    renderGroupsList();
    renderUsers();
    checkDataLoaded();
  }

  // Chunked CSV parsing for large files
  function parseCSVInChunks(file, config) {
    return new Promise((resolve, reject) => {
      const allResults = []; // Correctly initialize an array to store all results
      let rowCount = 0;
      let hasError = false;

      const parseConfig = {
        ...config,
        chunk: function (chunkResults, parser) {
          if (hasError) return;

          try {
            // Process chunk
            chunkResults.data.forEach(row => {
              if (row && Object.keys(row).some(key => row[key] && row[key].trim())) {
                allResults.push(row); // Push to the correctly initialized array
                rowCount++;
              }
            });

            // Update progress
            updateProgress(config.progressType || 'data', rowCount, rowCount + 1000);

            // Memory management: pause parsing if too many records loaded
            if (rowCount > 50000 && !config.allowLargeDatasets) {
              console.warn('Large dataset detected, pausing parsing...');
              parser.pause();
              setTimeout(() => parser.resume(), 100);
            }
          } catch (error) {
            hasError = true;
            reject(error);
          }
        },
        complete: function () {
          if (!hasError) {
            resolve({ data: allResults }); // Return the accumulated results
          }
        },
        error: function (error) {
          hasError = true;
          reject(error);
        }
      };

      Papa.parse(file, parseConfig);
    });
  }

  // Enhanced users data loading with error recovery
  async function loadUsersData() {
    console.log('Starting to load users data...');
    isLoading = true;
    const startTime = performance.now();
    try {
      const indexResponse = await fetch('/data/dashboard_index.json');
      if (indexResponse.ok) {
        const indexData = await indexResponse.json();
        applyDashboardIndex(indexData);
        performanceMetrics.loadTime = performance.now() - startTime;
        console.log('✅ Loaded compact dashboard index');
        return;
      }
    } catch (error) {
      console.log('Compact dashboard index not available, falling back to CSV');
    }

    // Hardcoded to only use /data/Users.csv for reliability
    const path = '/data/Users.csv';
    try {
      console.log(`Trying path: ${path}`);
      await parseCSVInChunks(path, {
        download: true,
        header: true,
        progressType: 'users',
        complete: (results) => {
          console.log('Users data loaded:', results.data);
          allUsers = results.data.filter(u => u.user_id && u.user_id.trim());
          console.log(`Filtered users: ${allUsers.length}`);
          console.log(`✅ Loaded ${allUsers.length} users from ${path}`);
          totalUsersEl.textContent = allUsers.length.toLocaleString();
          performanceMetrics.totalRecords += allUsers.length;
          performanceMetrics.loadTime = performance.now() - startTime;
          // Create index for better performance
          userIndex = createUserIndex();
          renderUsers();
          checkDataLoaded();
        }
      });
      // If we reach here, the file was loaded successfully
      return;
    } catch (error) {
      console.warn(`Failed to load from ${path}:`, error.message);
      handleError('users data from /data/Users.csv', error, () => {
        userGrid.innerHTML = `
          <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: #666; background: #f9f9f9; border-radius: 8px;">
            <h3>❌ No User Data Found</h3>
            <p>Please ensure Users.csv exists in the <b>data</b> folder at the project root.</p>
            <p><strong>Tried path:</strong> /data/Users.csv</p>
            <p><strong>Solution:</strong> Run <code>python -m toolkit.server.simple_server</code> and open <code>http://localhost:8000/web/dashboard.html</code></p>
            <button onclick="location.reload()" style="padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 5px; cursor: pointer;">Retry</button>
          </div>
        `;
      });
    }
  }

  // Load users data
  loadUsersData();

  // Enhanced memberships data loading
  async function loadMembershipsData() {
    if (userMembershipsIndex.size > 0) {
      checkDataLoaded();
      return;
    }
    // Hardcoded to only use /data/Memberships.csv for reliability
    const path = '/data/Memberships.csv';
    try {
      console.log(`Trying memberships path: ${path}`);
      await parseCSVInChunks(path, {
        download: true,
        header: true,
        progressType: 'memberships',
        complete: (results) => {
          memberships = results.data.filter(m => m.user_id && m.group_name);
          console.log(`✅ Loaded ${memberships.length} memberships from ${path}`);
          totalMembershipsEl.textContent = memberships.length.toLocaleString();
          performanceMetrics.totalRecords += memberships.length;
          calculateGroupCounts();
          renderGroupsList();
          checkDataLoaded();
        }
      });
      // If we reach here, the file was loaded successfully
      return;
    } catch (error) {
      console.warn(`Failed to load memberships from ${path}:`, error.message);
      handleError('memberships data from /data/Memberships.csv', error, () => {
        groupList.innerHTML = `
          <li style="color: #666; font-style: italic; text-align: center; padding: 20px;">
            <div>❌ No membership data found</div>
            <div style="margin: 10px 0; font-size: 0.9rem;">
              <strong>Tried path:</strong> /data/Memberships.csv
            </div>
            <div style="margin: 10px 0; font-size: 0.9rem;">
              <strong>Solution:</strong> Run <code>python -m toolkit.server.simple_server</code><br>
              Then open <code>http://localhost:8000/web/dashboard.html</code>
            </div>
            <button onclick="location.reload()" style="margin-top: 10px; padding: 8px 16px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer;">Retry</button>
          </li>
        `;
      });
    }
  }

  // Load memberships data
  loadMembershipsData();

  function checkDataLoaded() {
    if (allUsers.length > 0 || memberships.length > 0 || groupCounts.size > 0) {
      isLoading = false;
      loadingIndicator.style.display = 'none';

      // Show performance metrics in console
      console.log('Performance Metrics:', {
        totalRecords: performanceMetrics.totalRecords,
        loadTime: `${performanceMetrics.loadTime.toFixed(2)}ms`,
        renderTime: `${performanceMetrics.renderTime.toFixed(2)}ms`
      });

      // Show warning for large datasets
      if (performanceMetrics.totalRecords > 100000) {
        const warning = document.createElement('div');
        warning.style.cssText = `
          background: #fff3cd; 
          color: #856404; 
          padding: 10px; 
          margin: 10px; 
          border-radius: 5px; 
          border-left: 4px solid #ffc107;
          text-align: center;
        `;
        warning.innerHTML = `
          <strong>Large Dataset Detected:</strong> ${performanceMetrics.totalRecords.toLocaleString()} records loaded. 
          Performance may be slower on older devices.
        `;
        document.body.insertBefore(warning, document.body.firstChild);
        setTimeout(() => warning.remove(), 8000);
      }
    }
  }

  function calculateGroupCounts() {
    try {
      groupCounts.clear();

      // Use Map for better performance with large datasets
      const countMap = new Map();

      memberships.forEach(m => {
        const key = `${m.group_name} (${m.account || 'unknown'})`;
        countMap.set(key, (countMap.get(key) || 0) + 1);
      });

      groupCounts = countMap;
      totalGroupsEl.textContent = groupCounts.size.toLocaleString();
    } catch (error) {
      console.error('Error calculating group counts:', error);
      totalGroupsEl.textContent = '0';
    }
  }

  function renderGroupsList() {
    try {
      // Limit groups shown for performance
      const maxGroupsToShow = Math.min(groupCounts.size, 50);
      const sortedGroups = Array.from(groupCounts.entries())
        .sort((a, b) => b[1] - a[1]) // Sort by member count desc
        .slice(0, maxGroupsToShow);

      if (sortedGroups.length === 0) {
        groupList.innerHTML = '<li style="color: #666; font-style: italic; text-align: center; padding: 20px;">No group data available</li>';
        return;
      }

      const groupsHTML = sortedGroups.map(([groupName, count]) => `
        <li>
          <div class="group-info">
            <span class="group-name" title="${groupName}">${groupName}</span>
            <span class="group-count">${count.toLocaleString()}</span>
          </div>
        </li>
      `).join('');

      groupList.innerHTML = groupsHTML;

      // Add "show more" button if there are more groups
      if (groupCounts.size > maxGroupsToShow) {
        const moreButton = document.createElement('li');
        moreButton.style.cssText = 'text-align: center; padding: 10px;';
        moreButton.innerHTML = `
          <button onclick="showAllGroups()" style="padding: 8px 16px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer;">
            Show ${(groupCounts.size - maxGroupsToShow).toLocaleString()} More Groups
          </button>
        `;
        groupList.appendChild(moreButton);
      }
    } catch (error) {
      console.error('Error rendering groups list:', error);
      groupList.innerHTML = '<li style="color: #d32f2f;">Error loading groups data</li>';
    }
  }

  // Global function for "show more" button
  window.showAllGroups = function () {
    const sortedGroups = Array.from(groupCounts.entries())
      .sort((a, b) => b[1] - a[1]);

    groupList.innerHTML = sortedGroups.map(([groupName, count]) => `
      <li>
        <div class="group-info">
          <span class="group-name" title="${groupName}">${groupName}</span>
          <span class="group-count">${count.toLocaleString()}</span>
        </div>
      </li>
    `).join('');
  };

  function renderUsers() {
    console.log('Rendering users...');
    console.log('Filtered users:', filteredUsers);
    try {
      const renderStart = performance.now();

      // Use cached filtered results if available and letter hasn't changed
      if (filteredUsers.length === 0 || filteredUsers.letter !== currentLetter) {
        // Use index for better performance
        const indices = userIndex.get(currentLetter) || [];
        filteredUsers = indices.map(index => allUsers[index]);
        filteredUsers.letter = currentLetter;
      }

      const totalPages = Math.ceil(filteredUsers.length / usersPerPage);
      if (currentPage > totalPages) currentPage = totalPages || 1;

      const start = (currentPage - 1) * usersPerPage;
      const pageUsers = filteredUsers.slice(start, start + usersPerPage);

      // Use DocumentFragment for better performance
      const fragment = document.createDocumentFragment();

      if (pageUsers.length === 0) {
        const emptyDiv = document.createElement('div');
        emptyDiv.style.cssText = 'grid-column: 1/-1; text-align: center; padding: 40px; color: #666; background: #f9f9f9; border-radius: 8px;';
        emptyDiv.innerHTML = `
          <h3>No Users Found</h3>
          <p>No users found starting with "${currentLetter}"</p>
        `;
        fragment.appendChild(emptyDiv);
      } else {
        pageUsers.forEach(u => {
          const displayName = u.username || u.first_name || `User ${u.user_id}`;
          const subtitle = u.username ? (u.first_name || '') : (u.username || '');

          const userLink = document.createElement('a');
          userLink.href = '#';
          userLink.dataset.id = u.user_id;
          userLink.title = `ID: ${u.user_id}`;
          userLink.innerHTML = `
            <div style="font-weight: bold;">${displayName}</div>
            ${subtitle ? `<div style="font-size: 0.8em; color: #666;">${subtitle}</div>` : ''}
          `;
          fragment.appendChild(userLink);
        });
      }

      // Clear and populate grid
      userGrid.innerHTML = '';
      userGrid.appendChild(fragment);

      // Update pagination with better formatting
      const formatNumber = (num) => num.toLocaleString();
      pageIndicator.textContent = `Page ${formatNumber(currentPage)} of ${formatNumber(totalPages || 1)} (${formatNumber(filteredUsers.length)} users)`;

      prevBtn.disabled = currentPage === 1;
      nextBtn.disabled = currentPage === totalPages || totalPages === 0;

      const renderTime = performance.now() - renderStart;
      if (renderTime > 100) {
        console.warn(`Slow render: ${renderTime.toFixed(2)}ms for ${pageUsers.length} users`);
      }
    } catch (error) {
      console.error('Error rendering users:', error);
      userGrid.innerHTML = `
        <div style="grid-column: 1/-1; text-align: center; padding: 20px; color: #d32f2f; background: #ffebee; border-radius: 8px;">
          <strong>Error displaying users</strong><br>
          ${error.message}
        </div>
      `;
    }
  }

  prevBtn.addEventListener('click', () => {
    if (currentPage > 1 && !isLoading) {
      currentPage--;
      renderUsers();
    }
  });

  nextBtn.addEventListener('click', () => {
    if (!isLoading) {
      const totalPages = Math.ceil(filteredUsers.length / usersPerPage);
      if (currentPage < totalPages) {
        currentPage++;
        renderUsers();
      }
    }
  });

  // Keyboard navigation
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName !== 'INPUT' && !isLoading) {
      if (e.key === 'ArrowLeft' && !prevBtn.disabled) {
        currentPage--;
        renderUsers();
      } else if (e.key === 'ArrowRight' && !nextBtn.disabled) {
        const totalPages = Math.ceil(filteredUsers.length / usersPerPage);
        if (currentPage < totalPages) {
          currentPage++;
          renderUsers();
        }
      }
    }
  });

  // User interaction handling
  userGrid.addEventListener('click', (e) => {
    e.preventDefault();
    const userLink = e.target.closest('a[data-id]');
    if (userLink) {
      const userId = userLink.dataset.id;
      const user = allUsers.find(u => u.user_id === userId);
      if (user) {
        showUserDetails(user);
      }
    }
  });

  function showUserDetails(user) {
    // Find user's groups
    const userGroups = userMembershipsIndex.size > 0
      ? (userMembershipsIndex.get(String(user.user_id)) || []).map(m => m.group_name || 'Unknown')
      : memberships.filter(m => m.user_id === user.user_id).map(m => m.group_name);

    const modal = document.createElement('div');
    modal.style.cssText = `
      position: fixed; top: 0; left: 0; width: 100%; height: 100%; 
      background: rgba(0,0,0,0.5); z-index: 10000; 
      display: flex; justify-content: center; align-items: center;
    `;

    modal.innerHTML = `
      <div style="background: white; padding: 30px; border-radius: 10px; max-width: 500px; max-height: 70%; overflow-y: auto; box-shadow: 0 10px 30px rgba(0,0,0,0.3);">
        <h2 style="margin-top: 0; color: #333;">User Details</h2>
        <p><strong>ID:</strong> ${user.user_id}</p>
        <p><strong>Username:</strong> ${user.username || 'N/A'}</p>
        <p><strong>Name:</strong> ${((user.first_name || '') + ' ' + (user.last_name || '')).trim() || 'N/A'}</p>
        <p><strong>Phone:</strong> ${user.phone || 'N/A'}</p>
        <p><strong>Bot:</strong> ${user.is_bot === 'true' ? 'Yes' : 'No'}</p>
        <p><strong>Verified:</strong> ${user.is_verified === 'true' ? 'Yes' : 'No'}</p>
        <p><strong>Premium:</strong> ${user.is_premium === 'true' ? 'Yes' : 'No'}</p>
        <h3>Groups (${userGroups.length})</h3>
        <div style="max-height: 200px; overflow-y: auto; background: #f5f5f5; padding: 10px; border-radius: 5px;">
          ${userGroups.length > 0 ? userGroups.map(g => `<div style="margin-bottom: 5px; padding: 5px; background: white; border-radius: 3px;">${g}</div>`).join('') : '<em>No groups found</em>'}
        </div>
        <button onclick="this.closest('div[style*=\"position: fixed\"]').remove()" style="margin-top: 20px; padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 5px; cursor: pointer; width: 100%;">Close</button>
      </div>
    `;

    document.body.appendChild(modal);

    // Close on background click
    modal.addEventListener('click', (e) => {
      if (e.target === modal) {
        modal.remove();
      }
    });
  }

  // Initialize
  renderUsers();
});
