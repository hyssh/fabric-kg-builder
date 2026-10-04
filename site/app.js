'use strict';

const header = document.querySelector('.site-header');
const toggle = document.querySelector('.nav-toggle');
const nav = document.querySelector('#primary-nav');
const mobileQuery = window.matchMedia('(max-width: 760px)');

if (header && toggle && nav) {
  header.classList.add('nav-enhanced');
  toggle.hidden = !mobileQuery.matches;

  const closeNavigation = (returnFocus = false) => {
    nav.classList.remove('is-open');
    toggle.setAttribute('aria-expanded', 'false');
    if (returnFocus) toggle.focus();
  };

  toggle.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    nav.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', String(open));
  });
  nav.addEventListener('click', (event) => {
    if (event.target.closest('a')) closeNavigation();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') {
      closeNavigation(true);
    }
  });
  document.addEventListener('click', (event) => {
    if (!header.contains(event.target)) closeNavigation();
  });
  mobileQuery.addEventListener('change', () => {
    const focusWasInNav = nav.contains(document.activeElement);
    toggle.hidden = !mobileQuery.matches;
    closeNavigation(mobileQuery.matches && focusWasInNav);
  });
}

const status = document.querySelector('#copy-status');
let announcementTimer;
let announcementResetTimer;

const announce = (message) => {
  if (!status) return;
  window.clearTimeout(announcementTimer);
  window.clearTimeout(announcementResetTimer);
  status.textContent = '';
  announcementTimer = window.setTimeout(() => {
    status.textContent = message;
    announcementResetTimer = window.setTimeout(() => {
      status.textContent = '';
    }, 8000);
  }, 50);
};

document.querySelectorAll('.copy-button').forEach((button) => {
  const code = button.closest('.code-block')?.querySelector('pre code');
  if (!code) return;
  button.hidden = false;
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = 'Copying…';
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error('Clipboard access is unavailable.');
      }
      await navigator.clipboard.writeText(code.textContent);
      announce('Code copied to clipboard.');
    } catch {
      announce('Could not copy. Select the code and copy it manually with Ctrl+C or Command+C.');
    } finally {
      button.disabled = false;
      button.textContent = 'Copy';
    }
  });
});
