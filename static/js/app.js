// Secure OCR Platform - frontend logic
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

  let token = null;
  let currentUser = '';
  let selectedFile = null;

  const ENTITY_LABELS = {
    invoice_id: 'Invoice Number',
    contact_email: 'Email',
    phone_number: 'Phone',
    date: 'Date',
  };

  // ---------- Helpers ----------
  function showToast(msg, type) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show ' + (type || '');
    setTimeout(() => { t.classList.remove('show'); }, 3200);
  }

  async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const resp = await fetch('/api' + path, { ...options, headers });
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) {
      const msg = (data && data.error) || 'Request failed (' + resp.status + ')';
      throw new Error(msg);
    }
    return data;
  }

  function fmtConf(c) {
    if (c == null) return '-';
    return (c * 100).toFixed(0) + '%';
  }

  // ---------- Tabs ----------
  window.showTab = function (name) {
    $('#tabLogin').classList.toggle('active', name === 'login');
    $('#tabRegister').classList.toggle('active', name === 'register');
    $('#loginForm').classList.toggle('hidden', name !== 'login');
    $('#registerForm').classList.toggle('hidden', name !== 'register');
    $('#loginError').textContent = '';
    $('#registerError').textContent = '';
  };

  // ---------- Auth ----------
  window.handleLogin = async function (e) {
    e.preventDefault();
    const username = $('#loginUser').value.trim();
    const password = $('#loginPass').value;
    $('#loginError').textContent = '';
    try {
      const data = await api('/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      });
      token = data.access_token;
      currentUser = username;
      enterApp();
    } catch (err) {
      $('#loginError').textContent = err.message;
    }
    return false;
  };

  window.handleRegister = async function (e) {
    e.preventDefault();
    const username = $('#regUser').value.trim();
    const password = $('#regPass').value;
    const password2 = $('#regPass2').value;
    $('#registerError').textContent = '';
    if (password !== password2) {
      $('#registerError').textContent = 'Passwords do not match.';
      return false;
    }
    try {
      await api('/register', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      });
      showToast('Account created! You can now sign in.', 'success');
      $('#regUser').value = '';
      $('#regPass').value = '';
      $('#regPass2').value = '';
      showTab('login');
      $('#loginUser').value = username;
      $('#loginPass').focus();
    } catch (err) {
      $('#registerError').textContent = err.message;
    }
    return false;
  };

  function enterApp() {
    $('#authScreen').classList.add('hidden');
    $('#appScreen').classList.remove('hidden');
    $('#usernameLabel').textContent = currentUser;
    $('#avatarLetter').textContent = (currentUser[0] || '?').toUpperCase();
    loadDocuments();
  }

  window.logout = function () {
    token = null;
    selectedFile = null;
    $('#appScreen').classList.add('hidden');
    $('#authScreen').classList.remove('hidden');
    $('#loginPass').value = '';
    resetUpload();
    $('#docList').innerHTML = '<p class="muted">No documents yet.</p>';
    $('#docCount').textContent = '';
    $('#resultsPanel').classList.add('hidden');
    showToast('Signed out.', 'success');
  };

  // ---------- User Menu ----------
  window.toggleMenu = function (e) {
    e.stopPropagation();
    $('#userDropdown').classList.toggle('hidden');
  };
  document.addEventListener('click', function () {
    $('#userDropdown').classList.add('hidden');
  });

  // ---------- Modals ----------
  window.openPasswordModal = function () {
    $('#userDropdown').classList.add('hidden');
    $('#passwordError').textContent = '';
    $('#currentPass').value = '';
    $('#newPass').value = '';
    $('#confirmNewPass').value = '';
    $('#passwordModal').classList.remove('hidden');
  };

  window.openDeleteModal = function () {
    $('#userDropdown').classList.add('hidden');
    $('#deleteError').textContent = '';
    $('#deleteModal').classList.remove('hidden');
  };

  window.closeModal = function (id, event) {
    if (event && event.target !== event.currentTarget) return;
    $('#' + id).classList.add('hidden');
  };

  window.handleChangePassword = async function (e) {
    e.preventDefault();
    const oldp = $('#currentPass').value;
    const newp = $('#newPass').value;
    const confirmp = $('#confirmNewPass').value;
    $('#passwordError').textContent = '';
    if (newp !== confirmp) {
      $('#passwordError').textContent = 'New passwords do not match.';
      return false;
    }
    try {
      await api('/account/change-password', {
        method: 'POST',
        body: JSON.stringify({ old_password: oldp, new_password: newp }),
      });
      $('#passwordModal').classList.add('hidden');
      showToast('Password updated successfully.', 'success');
    } catch (err) {
      $('#passwordError').textContent = err.message;
    }
    return false;
  };

  window.handleDeleteAccount = async function () {
    $('#deleteError').textContent = '';
    try {
      await api('/account', { method: 'DELETE' });
      $('#deleteModal').classList.add('hidden');
      showToast('Account deleted.', 'success');
      logout();
    } catch (err) {
      $('#deleteError').textContent = err.message;
    }
  };

  // ---------- Upload ----------
  function handleFiles(files) {
    const file = files && files[0];
    if (!file) return;
    const okTypes = ['image/jpeg', 'image/png', 'image/webp'];
    if (!okTypes.includes(file.type)) {
      showToast('Unsupported file type. Use JPG, PNG or WebP.', 'error');
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      showToast('File too large. Max 5 MB.', 'error');
      return;
    }
    selectedFile = file;
    const reader = new FileReader();
    reader.onload = (e) => {
      const img = $('#previewImg');
      img.src = e.target.result;
      img.classList.remove('hidden');
    };
    reader.readAsDataURL(file);
    $('#scanBtn').disabled = false;
    $('#ocrError').textContent = '';
    $('#resultsPanel').classList.add('hidden');
  }

  window.handleFileSelect = function (e) { handleFiles(e.target.files); };
  window.handleDrop = function (e) {
    e.preventDefault();
    $('#dropzone').classList.remove('dragover');
    handleFiles(e.dataTransfer.files);
  };
  $('#fileInput').addEventListener('change', window.handleFileSelect);

  function resetUpload() {
    selectedFile = null;
    $('#fileInput').value = '';
    $('#previewImg').classList.add('hidden');
    $('#scanBtn').disabled = true;
  }

  // ---------- OCR ----------
  window.runOcr = async function () {
    if (!selectedFile) return;
    const btn = $('#scanBtn');
    btn.disabled = true;
    const label = btn.querySelector('.btn-label');
    const spinner = btn.querySelector('.spinner');
    label.textContent = 'Processing…';
    spinner.classList.remove('hidden');
    $('#ocrError').textContent = '';

    const fd = new FormData();
    fd.append('file', selectedFile, selectedFile.name);

    try {
      const resp = await fetch('/api/ocr', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token },
        body: fd,
      });
      let data = null;
      try { data = await resp.json(); } catch (e) {}
      if (!resp.ok) throw new Error((data && data.error) || 'OCR failed (' + resp.status + ')');

      renderResult(data);
      showToast('Text extracted successfully.', 'success');
      loadDocuments();
    } catch (err) {
      $('#ocrError').textContent = err.message;
    } finally {
      label.textContent = 'Extract Text';
      spinner.classList.add('hidden');
      btn.disabled = false;
    }
  };

  function renderResult(data) {
    $('#resultsPanel').classList.remove('hidden');
    $('#rawText').textContent = data.raw_text || '(no text detected)';

    const conf = data.average_confidence;
    const badge = $('#confidenceBadge');
    badge.textContent = 'Confidence ' + fmtConf(conf);
    badge.className = 'badge ' + (conf >= 0.75 ? 'high' : conf >= 0.5 ? 'medium' : 'low');

    $('#reviewWarning').classList.toggle('hidden', !data.review_warning);

    const box = $('#entities');
    box.innerHTML = '';
    const ents = data.structured_entities || {};
    const keys = Object.keys(ents);
    if (keys.length === 0) {
      box.innerHTML = '<div class="entity-empty">No structured entities detected.</div>';
      return;
    }
    keys.forEach((k) => {
      const item = document.createElement('div');
      item.className = 'entity-item';
      const label = ENTITY_LABELS[k] || k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
      item.innerHTML =
        '<span class="entity-key">' + escapeHtml(label) + '</span>' +
        '<span class="entity-value">' + escapeHtml(ents[k]) + '</span>';
      box.appendChild(item);
    });
  }

  // ---------- Documents ----------
  async function loadDocuments() {
    try {
      const data = await api('/documents', { method: 'GET' });
      const docs = data.documents || [];
      $('#docCount').textContent = docs.length + ' document' + (docs.length === 1 ? '' : 's');
      const list = $('#docList');
      list.innerHTML = '';
      if (docs.length === 0) {
        list.innerHTML = '<p class="muted">No documents yet.</p>';
        return;
      }
      docs.forEach((d) => {
        const card = document.createElement('div');
        card.className = 'doc-card';
        card.setAttribute('data-id', d.id);
        card.innerHTML =
          '<div class="doc-left">' +
          '<div class="doc-name">' + escapeHtml(d.filename) + '</div>' +
          '<div class="doc-meta">#' + d.id + ' &middot; Updated ' + escapeHtml(d.created_at || 'n/a') + '</div>' +
          '<div class="doc-meta">Confidence: <span class="conf">' + fmtConf(d.average_confidence) + '</span></div>' +
          '<div class="doc-text">' + escapeHtml(d.extracted_text) + '</div>' +
          '</div>' +
          '<div class="doc-actions">' +
          '<button class="btn-icon" onclick="viewDoc(' + d.id + ')">View</button>' +
          '<button class="btn-icon" onclick="deleteDoc(' + d.id + ')">Delete</button>' +
          '</div>';
        list.appendChild(card);
      });
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  window.viewDoc = function (id) {
    // Find the matching doc card and highlight it
    const card = document.querySelector('#docList').querySelector('[data-id="' + id + '"]');
    if (card) {
      document.querySelectorAll('.doc-card').forEach((c) => { c.style.borderColor = ''; });
      card.style.borderColor = 'var(--accent)';
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      showToast('Showing document #' + id, 'success');
    }
  };

  window.deleteDoc = async function (id) {
    try {
      await api('/documents/' + id, { method: 'DELETE' });
      showToast('Document deleted.', 'success');
      loadDocuments();
    } catch (err) {
      showToast(err.message, 'error');
    }
  };

  function escapeHtml(str) {
    if (str == null) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
  }
})();
