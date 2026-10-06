const state = { paperId: null, paper: null, selectedVersionId: null };
const statusLine = document.querySelector('#status');
const fetchButton = document.querySelector('#fetch-button');
const questionButton = document.querySelector('#question-button');
const activityPanel = document.querySelector('.activity');
const activityLog = document.querySelector('#activity-log');
const activityState = document.querySelector('#activity-state');

function beginActivity(label) {
  activityPanel.hidden = false;
  activityLog.replaceChildren();
  document.querySelector('#detected-title-panel').hidden = true;
  document.querySelector('#detected-title').textContent = '';
  activityState.textContent = label;
  activityState.className = 'working';
  statusLine.textContent = label;
}

function showStep(step) {
  if (step.extracted_title) {
    document.querySelector('#detected-title').textContent = step.extracted_title;
    document.querySelector('#detected-title-panel').hidden = false;
  }
  let row = activityLog.querySelector(`[data-step="${CSS.escape(step.id)}"]`);
  if (!row) {
    row = document.createElement('li');
    row.dataset.step = step.id;
    row.innerHTML = '<span class="step-icon" aria-hidden="true"></span><span class="step-copy"><strong></strong><small></small></span>';
    activityLog.append(row);
  }
  row.className = `activity-step ${step.state}`;
  row.querySelector('strong').textContent = step.label;
  row.querySelector('small').textContent = step.detail;
  activityState.textContent = step.state === 'running' ? step.label : (step.state === 'warning' ? 'Continuing with fallback' : step.state === 'failure' ? 'Failed' : 'In progress');
  activityState.className = step.state;
}

function failActivity(message) {
  if (activityLog.querySelector('.activity-step.failure')) {
    activityState.textContent = 'Failed';
    activityState.className = 'failure';
    return;
  }
  const row = document.createElement('li');
  row.className = 'activity-step failure';
  row.innerHTML = '<span class="step-icon" aria-hidden="true"></span><span class="step-copy"><strong>Operation failed</strong><small></small></span>';
  row.querySelector('small').textContent = message;
  activityLog.append(row);
  activityState.textContent = 'Failed';
  activityState.className = 'failure';
}

async function requestProgress(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || `Request failed (${response.status})`);
  }
  if (!response.body) throw new Error('This browser cannot display live processing updates.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let result = null;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const events = buffer.split('\n\n');
    buffer = events.pop() || '';
    for (const event of events) {
      const line = event.split('\n').find(item => item.startsWith('data: '));
      if (!line) continue;
      const message = JSON.parse(line.slice(6));
      if (message.type === 'step') showStep(message);
      if (message.type === 'failure') {
        showStep({ id: message.id, label: message.label, state: 'failure', detail: message.detail });
        throw new Error(message.detail);
      }
      if (message.type === 'result') result = message.result;
    }
    if (done) break;
  }
  if (!result) throw new Error('The server ended the progress stream without a result.');
  activityState.textContent = 'Complete';
  activityState.className = 'success';
  return result;
}
function setStatus(message, isError = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle('error', isError);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === 'object' ? body.detail.message : body.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return body;
}

function renderExtraction(fields) {
  const grid = document.querySelector('#extraction-grid');
  grid.replaceChildren();
  fields.forEach(({ field_type: name, value, section_heading: sectionHeading }) => {
    const card = document.createElement('article'); card.className = 'field';
    const title = document.createElement('h3'); title.textContent = name.replaceAll('_', ' '); card.append(title);
    if (sectionHeading) {
      const source = document.createElement('p'); source.className = 'field-source';
      source.textContent = `Source section: ${sectionHeading}`; card.append(source);
    }
    const values = value.items || (value.text ? [value.text] : []);
    if (values.length) { const list = document.createElement('ul'); values.forEach(item => { const li = document.createElement('li'); li.textContent = typeof item === 'string' ? item : item.statement; list.append(li); }); card.append(list); }
    else { const empty = document.createElement('p'); empty.textContent = 'Not identified in this section.'; card.append(empty); }
    grid.append(card);
  });
}

function renderSections(sections) {
  const container = document.querySelector('#paper-sections');
  container.replaceChildren();
  sections.forEach(section => {
    const details = document.createElement('details'); details.className = 'paper-section';
    const summary = document.createElement('summary'); summary.textContent = section.heading || `Section ${section.position + 1}`;
    const text = document.createElement('pre'); text.textContent = section.content;
    details.append(summary, text); container.append(details);
  });
  document.querySelector('#sections-details').open = false;
}

function formatVersionDate(timestamp) {
  if (!timestamp) return '';
  const milliseconds = timestamp < 100000000000 ? timestamp * 1000 : timestamp;
  const date = new Date(milliseconds);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
}

function renderReviews(reviews, version) {
  const container = document.querySelector('#paper-reviews');
  container.replaceChildren();
  const count = reviews?.length || 0;
  document.querySelector('#review-count').textContent = `${count} ${count === 1 ? 'review' : 'reviews'} · ${version.version_key}`;
  document.querySelector('#reviews-empty').hidden = count > 0;
  (reviews || []).forEach((review, index) => {
    const details = document.createElement('details');
    details.className = 'paper-review';
    const summary = document.createElement('summary');
    const date = review.written_at ? new Date(review.written_at).toLocaleDateString() : '';
    summary.textContent = `${review.invitation || `Review ${index + 1}`}${date ? ` · ${date}` : ''}`;
    const text = document.createElement('pre');
    text.textContent = review.review_text || '(No review text)';
    details.append(summary, text);
    container.append(details);
  });
}

function renderRevision(data, version) {
  state.selectedVersionId = version.id;
  document.querySelector('#revision-select').value = version.id;
  if (version.source_forum_id) {
    document.querySelector('#source-link').href = `https://openreview.net/forum?id=${encodeURIComponent(version.source_forum_id)}`;
  }
  document.querySelector('#paper-title').textContent = data.title || version.title || 'Untitled paper';
  document.querySelector('#paper-authors').textContent = (data.authors || version.authors || []).join(', ') || 'Authors unavailable';
  const date = formatVersionDate(version.version_timestamp);
  const latest = version.is_latest ? ' · newest overall' : ' · latest in source forum';
  document.querySelector('#revision-context').textContent = `${version.version_key}${version.source_forum_id ? ` · source forum ${version.source_forum_id}` : ''}${date ? ` · ${date}` : ''}${latest} · ${version.text_characters.toLocaleString()} text characters`;
  const abstractSection = (data.sections || []).find(section => (section.heading || '').toLowerCase() === 'abstract');
  document.querySelector('#abstract').textContent = data.abstract || abstractSection?.content || 'No abstract stored.';
  renderSections(data.sections || []);
  renderExtraction(data.extracted_fields || []);
  document.querySelector('#extraction-context').textContent = version.is_latest
    ? 'Structured extraction is associated with the latest revision.'
    : 'This older revision is stored with its own text, sections, and reviews. Structured Gemini extraction currently runs on the latest revision only.';
  renderReviews(data.reviews || [], version);
  document.querySelector('#questions').replaceChildren();
}

function renderPaper(paper) {
  state.paperId = paper.id;
  state.paper = paper;
  document.querySelector('#paper-view').hidden = false;
  const sourceLink = document.querySelector('#source-link');
  sourceLink.href = paper.forum_id ? `https://openreview.net/forum?id=${encodeURIComponent(paper.forum_id)}` : paper.source_uri;
  sourceLink.hidden = paper.source_type === 'upload';
  const versions = [...(paper.versions || [])].sort((a, b) => (b.version_timestamp || 0) - (a.version_timestamp || 0));
  const select = document.querySelector('#revision-select');
  select.replaceChildren();
  versions.forEach(version => {
    const option = document.createElement('option');
    option.value = version.id;
    option.textContent = `${version.is_latest ? 'Newest overall · ' : 'Latest in forum · '}${version.version_key}${version.source_forum_id ? ` · forum ${version.source_forum_id}` : ''}${formatVersionDate(version.version_timestamp) ? ` · ${formatVersionDate(version.version_timestamp)}` : ''} · ${version.review_count} reviews`;
    select.append(option);
  });
  const latest = versions.find(version => version.id === paper.latest_version_id) || versions[0];
  if (latest) renderRevision(paper, latest);
  else {
    document.querySelector('#paper-title').textContent = paper.title || 'Untitled paper';
    document.querySelector('#paper-authors').textContent = (paper.authors || []).join(', ');
    document.querySelector('#abstract').textContent = 'No abstract stored.';
    renderSections([]); renderExtraction([]); renderReviews([], { version_key: 'No revision' });
  }
}

document.querySelector('#revision-select').addEventListener('change', async event => {
  if (!state.paperId || !event.target.value) return;
  const version = state.paper.versions.find(item => item.id === event.target.value);
  if (!version) return;
  if (version.id === state.paper.latest_version_id) {
    renderRevision(state.paper, version);
    return;
  }
  event.target.disabled = true;
  try {
    const detail = await request(`/api/v1/papers/${state.paperId}/versions/${version.id}`);
    renderRevision(detail, version);
  } catch (error) {
    setStatus(`Could not load revision ${version.version_key}: ${error.message}`, true);
    event.target.value = state.selectedVersionId || state.paper.latest_version_id;
  } finally {
    event.target.disabled = false;
  }
});

document.querySelector('#ingest-form').addEventListener('submit', async event => {
  event.preventDefault();
  const forumId = document.querySelector('#forum-id').value.trim();
  fetchButton.disabled = true; beginActivity('Processing forum paper…');
  try { const paper = await requestProgress('/api/v1/papers/openreview/progress', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({forum_id: forumId}) }); renderPaper(paper); setStatus(paper.from_cache ? 'Loaded the stored paper.' : 'Paper fetched, extracted, and saved.'); }
  catch (error) { failActivity(error.message); setStatus(error.message, true); }
  finally { fetchButton.disabled = false; }
});

document.querySelector('#upload-form').addEventListener('submit', async event => {
  event.preventDefault();
  const file = document.querySelector('#paper-file').files[0];
  if (!file) return;
  const button = document.querySelector('#upload-button');
  button.disabled = true; beginActivity(`Uploading ${file.name}…`);
  try {
    const form = new FormData(); form.append('file', file);
    const paper = await requestProgress('/api/v1/papers/upload/progress', { method: 'POST', body: form });
    renderPaper(paper);
    const matched = paper.raw_metadata?.forum_id;
    setStatus(paper.from_cache ? 'Loaded the stored paper.' : matched ? `Matched OpenReview forum ${matched}; paper fetched and analyzed.` : 'Analyzed the uploaded paper; no OpenReview record was used.');
  } catch (error) { failActivity(error.message); setStatus(error.message, true); }
  finally { button.disabled = false; }
});

questionButton.addEventListener('click', async () => {
  if (!state.paperId) return;
  questionButton.disabled = true; beginActivity('Generating discussion questions…');
  try { const result = await requestProgress(`/api/v1/papers/${state.paperId}/questions/progress`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({count:5}) }); const list = document.querySelector('#questions'); list.replaceChildren(); result.questions.forEach(question => { const li=document.createElement('li'); li.textContent=question.text; list.append(li); }); setStatus('Questions generated and saved.'); }
  catch (error) { failActivity(error.message); setStatus(error.message, true); }
  finally { questionButton.disabled = false; }
});
