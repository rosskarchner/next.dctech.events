(function() {
  let posts = [];
  let selectedSlug = null;   // null while composing a not-yet-saved post
  let isNew = false;

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function slugify(text) {
    return String(text || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }

  function today() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function el(id) {
    return document.getElementById(id);
  }

  function showMessage(text, kind) {
    const box = el('post-message');
    if (!text) {
      box.innerHTML = '';
      return;
    }
    box.innerHTML =
      `<div class="message message-${kind}"><p>${escapeHtml(text)}</p></div>`;
    if (kind === 'success') {
      setTimeout(() => { box.innerHTML = ''; }, 4000);
    }
  }

  function renderList() {
    const list = el('post-list');
    if (!posts.length) {
      list.innerHTML = '<li><div class="empty-state">No posts yet.</div></li>';
      return;
    }

    list.innerHTML = posts.map((post) => {
      const badge = post.status === 'published'
        ? '<span class="badge badge-published">Published</span>'
        : '<span class="badge badge-draft">Draft</span>';
      const selected = post.slug === selectedSlug ? ' selected' : '';
      return `
        <li>
          <button type="button" class="post-row${selected}" data-slug="${escapeHtml(post.slug)}">
            <span class="post-title">${escapeHtml(post.title || post.slug)}</span>
            <span class="post-meta">${escapeHtml(post.published_on || '')}${badge}</span>
          </button>
        </li>`;
    }).join('');

    list.querySelectorAll('.post-row').forEach((button) => {
      button.addEventListener('click', () => selectPost(button.dataset.slug));
    });
  }

  function showEditor(show) {
    el('post-form').classList.toggle('hidden', !show);
    el('editor-empty').classList.toggle('hidden', show);
  }

  function fillForm(post) {
    el('post-title').value = post.title || '';
    el('post-slug').value = post.slug || '';
    el('post-date').value = post.published_on || today();
    el('post-status').value = post.status || 'draft';
    el('post-summary').value = post.summary || '';
    el('post-body').value = post.body || '';
    updateSlugPreview();
  }

  function updateSlugPreview() {
    const explicit = el('post-slug').value.trim();
    const derived = explicit || slugify(el('post-title').value) || 'your-slug';
    el('slug-preview').textContent = derived;
  }

  function selectPost(slug) {
    const post = posts.find((p) => p.slug === slug);
    if (!post) return;
    selectedSlug = slug;
    isNew = false;
    fillForm(post);
    // Deleting only makes sense for a post that exists server-side.
    el('delete-post').classList.remove('hidden');
    showEditor(true);
    renderList();
  }

  function startNewPost() {
    selectedSlug = null;
    isNew = true;
    fillForm({ published_on: today(), status: 'draft' });
    el('delete-post').classList.add('hidden');
    showEditor(true);
    renderList();
    el('post-title').focus();
  }

  function cancelEdit() {
    selectedSlug = null;
    isNew = false;
    showEditor(false);
    renderList();
  }

  async function request(path, options) {
    const response = await DctechAuth.authorizedFetch(path, options);
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      // A non-JSON body (gateway error page) still needs a usable message.
    }
    if (!response.ok) {
      throw new Error(payload.error || `Request failed (HTTP ${response.status})`);
    }
    return payload;
  }

  async function loadPosts(selectAfter) {
    el('posts-loading').style.display = 'block';
    try {
      const data = await request('/api/admin/posts');
      posts = data.posts || [];
      el('posts-loading').style.display = 'none';
      renderList();
      if (selectAfter) {
        selectPost(selectAfter);
      }
    } catch (error) {
      el('posts-loading').style.display = 'none';
      showMessage(`Could not load posts: ${error.message}`, 'error');
    }
  }

  function formPayload() {
    return {
      title: el('post-title').value.trim(),
      slug: el('post-slug').value.trim() || slugify(el('post-title').value),
      published_on: el('post-date').value || today(),
      status: el('post-status').value,
      summary: el('post-summary').value.trim(),
      body: el('post-body').value,
    };
  }

  async function savePost(event) {
    event.preventDefault();
    const payload = formPayload();
    if (!payload.title) {
      showMessage('A title is required.', 'error');
      return;
    }

    const button = el('save-post');
    button.disabled = true;
    button.textContent = 'Saving…';

    try {
      const result = isNew
        ? await request('/api/admin/posts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          })
        : await request(`/api/admin/posts/${encodeURIComponent(selectedSlug)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });

      const saved = result.post || {};
      isNew = false;
      showMessage(
        saved.status === 'published'
          ? 'Saved. It will appear on /updates after the next rebuild.'
          : 'Draft saved. It stays hidden until you publish it.',
        'success'
      );
      await loadPosts(saved.slug);
    } catch (error) {
      showMessage(error.message, 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'Save';
    }
  }

  async function deletePost() {
    if (!selectedSlug) return;
    const post = posts.find((p) => p.slug === selectedSlug);
    const label = post ? (post.title || post.slug) : selectedSlug;
    if (!window.confirm(`Delete "${label}"? This cannot be undone.`)) {
      return;
    }

    try {
      await request(`/api/admin/posts/${encodeURIComponent(selectedSlug)}`, {
        method: 'DELETE',
      });
      showMessage('Post deleted.', 'success');
      cancelEdit();
      await loadPosts();
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  function init() {
    // Without this a non-admin sees the whole editor and every request 403s.
    // The server was always safe (_admin_check on each route); the page was
    // just misleading.
    if (!DctechAuth.requireAdmin()) return;
    el('new-post').addEventListener('click', startNewPost);
    el('cancel-edit').addEventListener('click', cancelEdit);
    el('delete-post').addEventListener('click', deletePost);
    el('post-form').addEventListener('submit', savePost);
    el('post-title').addEventListener('input', updateSlugPreview);
    el('post-slug').addEventListener('input', updateSlugPreview);
    loadPosts();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
