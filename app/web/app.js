const state = { paperId: null, paper: null, selectedVersionId: null };
const themeToggle = document.querySelector('#theme-toggle');
const statusLine = document.querySelector('#status');
const fetchButton = document.querySelector('#fetch-button');
const questionButton = document.querySelector('#question-button');
const agenticQuestionButton = document.querySelector('#agentic-question-button');
const activityPanel = document.querySelector('.activity');
const activityLog = document.querySelector('#activity-log');
const activityState = document.querySelector('#activity-state');

function applyTheme(theme, persist = true) {
  const dark = theme === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  themeToggle.textContent = dark ? 'Light mode' : 'Dark mode';
  themeToggle.setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} mode`);
  themeToggle.setAttribute('aria-pressed', String(dark));
  if (persist) localStorage.setItem('paperprobe-theme', dark ? 'dark' : 'light');
}

applyTheme(document.documentElement.dataset.theme || 'light', false);
themeToggle.addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});

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

agenticQuestionButton.addEventListener('click', async () => {
  if (!state.paperId) return;
  agenticQuestionButton.disabled = true;
  questionButton.disabled = true;
  beginActivity('Starting the agentic question workflow…');
  activityPanel.scrollIntoView({ behavior: 'smooth', block: 'center' });
  try {
    const result = await requestProgress(
      `/api/v1/papers/${state.paperId}/agentic-questions/progress`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: 5 }),
      },
    );
    const list = document.querySelector('#questions');
    list.replaceChildren();
    (result.questions || []).forEach(question => {
      const item = document.createElement('li');
      item.textContent = question.text;
      list.append(item);
    });
    setStatus(result.saved_count
      ? `Agentic workflow saved ${result.saved_count} of ${result.requested_count} requested questions.`
      : 'The agent completed its critique loop; no questions met the quality bar.');
  } catch (error) {
    failActivity(error.message);
    setStatus(error.message, true);
  } finally {
    agenticQuestionButton.disabled = false;
    questionButton.disabled = false;
  }
});

const exampleForm = document.querySelector('#example-upload-form');
const exampleQuestionRows = document.querySelector('#example-question-rows');
const exampleUploadStatus = document.querySelector('#example-upload-status');
const addExampleQuestionButton = document.querySelector('#add-example-question');
const MAX_EXAMPLE_QUESTIONS = 20;

function syncExampleQuestionRows() {
  const rows = [...exampleQuestionRows.querySelectorAll('.example-question-row')];
  rows.forEach(row => { row.querySelector('.remove-example-question').disabled = rows.length === 1; });
  addExampleQuestionButton.disabled = rows.length >= MAX_EXAMPLE_QUESTIONS;
}

function addExampleQuestionRow() {
  if (exampleQuestionRows.querySelectorAll('.example-question-row').length >= MAX_EXAMPLE_QUESTIONS) return;
  const row = document.createElement('div');
  row.className = 'example-question-row';
  const questionField = document.createElement('div');
  const questionLabel = document.createElement('label');
  questionLabel.textContent = 'Question';
  const question = document.createElement('textarea');
  question.className = 'example-question-text';
  question.required = true;
  question.rows = 3;
  question.placeholder = 'Enter a question grounded in this paper';
  questionField.append(questionLabel, question);

  const rationaleField = document.createElement('div');
  const rationaleLabel = document.createElement('label');
  rationaleLabel.textContent = 'Rationale (optional)';
  const rationale = document.createElement('textarea');
  rationale.className = 'example-question-rationale';
  rationale.rows = 2;
  rationale.placeholder = 'Why is this a useful question?';
  rationaleField.append(rationaleLabel, rationale);

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'remove-example-question';
  remove.setAttribute('aria-label', 'Remove question');
  remove.title = 'Remove question';
  remove.textContent = '×';
  row.append(questionField, rationaleField, remove);
  exampleQuestionRows.append(row);
  syncExampleQuestionRows();
  question.focus();
}

addExampleQuestionButton.addEventListener('click', addExampleQuestionRow);
exampleQuestionRows.addEventListener('click', event => {
  const remove = event.target.closest('.remove-example-question');
  if (!remove || exampleQuestionRows.querySelectorAll('.example-question-row').length <= 1) return;
  remove.closest('.example-question-row').remove();
  syncExampleQuestionRows();
});

exampleForm.addEventListener('submit', async event => {
  event.preventDefault();
  const file = document.querySelector('#example-paper-file').files[0];
  if (!file) return;
  const rows = [...exampleQuestionRows.querySelectorAll('.example-question-row')];
  const submitButton = document.querySelector('#save-example-pairs');
  submitButton.disabled = true;
  addExampleQuestionButton.disabled = true;
  exampleUploadStatus.className = 'example-upload-status working';
  exampleUploadStatus.textContent = 'Reading the paper and saving its question examples…';
  try {
    const form = new FormData();
    form.append('file', file);
    form.append('source_type', document.querySelector('#example-source-type').value);
    rows.forEach(row => {
      form.append('questions', row.querySelector('.example-question-text').value.trim());
      form.append('rationales', row.querySelector('.example-question-rationale').value.trim());
    });
    const result = await request('/api/v1/examples/upload', { method: 'POST', body: form });
    exampleUploadStatus.className = 'example-upload-status success';
    exampleUploadStatus.textContent = `Saved ${result.question_count} ${result.question_count === 1 ? 'question' : 'questions'} for “${result.source_title}”.`;
    exampleForm.reset();
    exampleQuestionRows.replaceChildren();
    addExampleQuestionRow();
    loadExampleLibrary();
  } catch (error) {
    exampleUploadStatus.className = 'example-upload-status error';
    exampleUploadStatus.textContent = error.message;
  } finally {
    submitButton.disabled = false;
    syncExampleQuestionRows();
  }
});

syncExampleQuestionRows();

const savedExamplePapers = document.querySelector('#saved-example-papers');
const savedExampleStatus = document.querySelector('#saved-example-status');
const refreshExampleLibraryButton = document.querySelector('#refresh-example-library');

function makeLabeledTextarea(labelText, value, className, placeholder = '') {
  const field = document.createElement('div');
  const textarea = document.createElement('textarea');
  const id = `saved-example-${crypto.randomUUID()}`;
  const label = document.createElement('label');
  label.htmlFor = id;
  label.textContent = labelText;
  textarea.id = id;
  textarea.className = className;
  textarea.value = value || '';
  textarea.rows = className.includes('question-text') ? 3 : 2;
  textarea.maxLength = 10000;
  textarea.placeholder = placeholder;
  field.append(label, textarea);
  return { field, textarea };
}

function renderSavedExamplePaper(paper) {
  const article = document.createElement('article');
  article.className = 'saved-example-paper';
  const heading = document.createElement('div');
  heading.className = 'saved-example-paper-heading';
  const title = document.createElement('h4');
  title.textContent = paper.source_title;
  const source = document.createElement('span');
  source.className = 'saved-example-source';
  source.textContent = paper.source_type === 'own_criticism' ? 'My own criticism' : 'OpenReview review';
  heading.append(title, source);
  article.append(heading);

  const contextDetails = document.createElement('details');
  contextDetails.className = 'saved-example-context';
  const contextSummary = document.createElement('summary');
  contextSummary.textContent = 'Source paper context';
  const context = document.createElement('p');
  context.textContent = paper.source_abstract_or_claims;
  contextDetails.append(contextSummary, context);
  article.append(contextDetails);

  const questionHeading = document.createElement('h5');
  questionHeading.textContent = `Questions (${paper.questions.length})`;
  article.append(questionHeading);
  const questionList = document.createElement('div');
  questionList.className = 'saved-example-question-list';
  for (const question of paper.questions) {
    const row = document.createElement('article');
    row.className = 'saved-example-question';
    const questionField = makeLabeledTextarea('Question', question.question_text, 'saved-question-text');
    const rationaleField = makeLabeledTextarea('Rationale (optional)', question.rationale, 'saved-question-rationale');
    const actions = document.createElement('div');
    actions.className = 'saved-example-actions';
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'secondary-button';
    save.textContent = 'Save changes';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-saved-example';
    remove.textContent = 'Remove question';
    const message = document.createElement('span');
    message.className = 'saved-example-row-status';
    message.setAttribute('role', 'status');
    save.addEventListener('click', async () => {
      save.disabled = true;
      message.textContent = 'Saving…';
      try {
        await request(`/api/v1/examples/questions/${question.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            question_text: questionField.textarea.value,
            rationale: rationaleField.textarea.value,
          }),
        });
        message.textContent = 'Saved.';
      } catch (error) {
        message.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    remove.addEventListener('click', async () => {
      if (!window.confirm('Remove this question from the saved paper?')) return;
      remove.disabled = true;
      try {
        await request(`/api/v1/examples/questions/${question.id}`, { method: 'DELETE' });
        await loadExampleLibrary();
      } catch (error) {
        message.textContent = error.message;
        remove.disabled = false;
      }
    });
    actions.append(save, remove, message);
    row.append(questionField.field, rationaleField.field, actions);
    questionList.append(row);
  }
  article.append(questionList);

  const addForm = document.createElement('form');
  addForm.className = 'saved-example-add-form';
  const addHeading = document.createElement('h5');
  addHeading.textContent = 'Add a question';
  const newQuestionField = makeLabeledTextarea('Question', '', 'new-saved-question-text', 'Enter a question for this paper');
  newQuestionField.textarea.required = true;
  const newRationaleField = makeLabeledTextarea('Rationale (optional)', '', 'new-saved-question-rationale', 'Why is this a useful question?');
  const addActions = document.createElement('div');
  addActions.className = 'saved-example-actions';
  const addButton = document.createElement('button');
  addButton.type = 'submit';
  addButton.className = 'secondary-button';
  addButton.textContent = 'Add question';
  const addMessage = document.createElement('span');
  addMessage.className = 'saved-example-row-status';
  addMessage.setAttribute('role', 'status');
  addActions.append(addButton, addMessage);
  addForm.append(addHeading, newQuestionField.field, newRationaleField.field, addActions);
  addForm.addEventListener('submit', async event => {
    event.preventDefault();
    addButton.disabled = true;
    addMessage.textContent = 'Adding…';
    try {
      await request(`/api/v1/examples/papers/${paper.source_paper_id}/questions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question_text: newQuestionField.textarea.value,
          rationale: newRationaleField.textarea.value,
        }),
      });
      await loadExampleLibrary();
    } catch (error) {
      addMessage.textContent = error.message;
      addButton.disabled = false;
    }
  });
  article.append(addForm);
  return article;
}

async function loadExampleLibrary() {
  refreshExampleLibraryButton.disabled = true;
  savedExampleStatus.className = 'example-upload-status';
  savedExampleStatus.textContent = 'Loading saved papers…';
  try {
    const result = await request('/api/v1/examples');
    savedExamplePapers.replaceChildren();
    if (!result.papers.length) {
      savedExampleStatus.textContent = 'No saved example papers yet.';
      return;
    }
    result.papers.forEach(paper => savedExamplePapers.append(renderSavedExamplePaper(paper)));
    savedExampleStatus.textContent = `${result.papers.length} saved ${result.papers.length === 1 ? 'paper' : 'papers'}.`;
  } catch (error) {
    savedExampleStatus.className = 'example-upload-status error';
    savedExampleStatus.textContent = error.message;
  } finally {
    refreshExampleLibraryButton.disabled = false;
  }
}

refreshExampleLibraryButton.addEventListener('click', loadExampleLibrary);
loadExampleLibrary();
