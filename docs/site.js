(function () {
  'use strict';

  const CHICKEN_PER_ORDER = 400;

  function initialState() {
    return {
      orderStatus: 'empty',
      paused: false,
      events: [],
      appliedEventIds: [],
      nextEventId: 1,
      inventory: { onHand: 5000, reserved: 0, used: 0 }
    };
  }

  function copyState(state) {
    return {
      ...state,
      events: state.events.map(event => ({ ...event })),
      appliedEventIds: [...state.appliedEventIds],
      inventory: { ...state.inventory }
    };
  }

  function applyWaitingChanges(state) {
    if (state.paused) return state;
    const applied = new Set(state.appliedEventIds);
    state.events.forEach(event => {
      if (applied.has(event.id)) return;
      if (event.type === 'ordered') state.inventory.reserved += CHICKEN_PER_ORDER;
      if (event.type === 'prepared') {
        state.inventory.reserved -= CHICKEN_PER_ORDER;
        state.inventory.onHand -= CHICKEN_PER_ORDER;
        state.inventory.used += CHICKEN_PER_ORDER;
      }
      if (event.type === 'cancelled') state.inventory.reserved -= CHICKEN_PER_ORDER;
      applied.add(event.id);
      state.appliedEventIds.push(event.id);
    });
    return state;
  }

  function reduce(current, action) {
    if (!action || typeof action.type !== 'string') return current;
    if (action.type === 'reset') return initialState();
    const transitions = {
      place: { from: 'empty', to: 'queued', event: 'ordered' },
      prepare: { from: 'queued', to: 'preparing', event: 'prepared' },
      ready: { from: 'preparing', to: 'ready', event: 'ready' },
      cancel: { from: 'queued', to: 'cancelled', event: 'cancelled' }
    };
    if (action.type === 'pause') {
      if (current.paused) return current;
      const next = copyState(current);
      next.paused = true;
      return next;
    }
    if (action.type === 'resume') {
      const next = copyState(current);
      next.paused = false;
      return applyWaitingChanges(next);
    }
    const transition = transitions[action.type];
    if (!transition || current.orderStatus !== transition.from) return current;
    const next = copyState(current);
    next.orderStatus = transition.to;
    next.events.push({ id: next.nextEventId, type: transition.event });
    next.nextEventId += 1;
    return applyWaitingChanges(next);
  }

  let state = initialState();
  const byId = id => document.getElementById(id);
  const formatGrams = amount => amount.toLocaleString('en-GB') + ' g';

  function setText(id, text) {
    const element = byId(id);
    if (element && element.textContent !== text) element.textContent = text;
  }

  function setDisabled(id, disabled) {
    const element = byId(id);
    if (element) element.disabled = disabled;
  }

  function render() {
    const pending = state.events.filter(event => !state.appliedEventIds.includes(event.id)).length;
    const labels = { empty: 'Waiting for order', queued: 'Queued', preparing: 'Preparing', ready: 'Ready for service', cancelled: 'Cancelled' };
    const instructions = {
      empty: 'Send the order to create a kitchen ticket and reserve its recipe ingredients.',
      queued: 'The kitchen has its ticket. Start preparing to turn the chicken reservation into recorded recipe usage.',
      preparing: 'Preparation has started. Marking the dish ready will not use another 400 g of chicken.',
      ready: pending
        ? 'Ready for Table 7. The stock changes will appear when sample updates resume.'
        : 'Ready for Table 7. Recipe usage was recorded once. Reset the example to try another path.',
      cancelled: 'The queued order is cancelled. Its reservation is released when sample updates are applied.'
    };
    setText('demo-status', labels[state.orderStatus]);
    setText('demo-step-note', instructions[state.orderStatus]);
    setText('demo-on-hand', formatGrams(state.inventory.onHand));
    setText('demo-reserved', formatGrams(state.inventory.reserved));
    setText('demo-available', formatGrams(state.inventory.onHand - state.inventory.reserved));
    setText('demo-used', formatGrams(state.inventory.used));
    setText('demo-sync-status', state.paused ? 'Paused · not current' : 'Sample updates on');
    setText('demo-pending', pending ? pending + ' ' + (pending === 1 ? 'change waiting' : 'changes waiting') : 'No changes waiting');
    setText('demo-sync', state.paused ? 'Resume sample updates' : 'Pause sample updates');
    setText('demo-sync-note', state.paused
      ? 'Sample inventory is paused. These figures are not current. The kitchen can still progress; resume to apply its waiting changes once.'
      : 'The recipe estimate follows the order. This is a simulation, not live stock.');
    setDisabled('demo-place', state.orderStatus !== 'empty');
    setDisabled('demo-prepare', state.orderStatus !== 'queued');
    setDisabled('demo-ready', state.orderStatus !== 'preparing');
    setDisabled('demo-cancel', state.orderStatus !== 'queued');
    const syncButton = byId('demo-sync');
    if (syncButton) syncButton.setAttribute('aria-pressed', String(state.paused));
    const root = byId('demo-root');
    if (root) root.dataset.state = state.orderStatus;
    const inventoryPanel = document.querySelector('.demo-inventory');
    if (inventoryPanel) inventoryPanel.classList.toggle('is-paused', state.paused);
    const list = byId('demo-event-list');
    if (list) {
      list.replaceChildren();
      const eventLabels = { ordered: 'Order sent', prepared: 'Preparation started', ready: 'Marked ready', cancelled: 'Queued order cancelled' };
      if (!state.events.length) {
        const item = document.createElement('li');
        item.textContent = 'No order sent yet.';
        list.append(item);
      }
      state.events.forEach(event => {
        const item = document.createElement('li');
        item.textContent = eventLabels[event.type];
        list.append(item);
      });
    }
  }

  function dispatch(type) {
    state = reduce(state, { type });
    render();
  }

  [['demo-place', 'place'], ['demo-prepare', 'prepare'], ['demo-ready', 'ready'], ['demo-cancel', 'cancel'], ['demo-reset', 'reset']].forEach(([id, type]) => {
    const button = byId(id);
    if (button) button.addEventListener('click', () => dispatch(type));
  });
  const syncButton = byId('demo-sync');
  if (syncButton) syncButton.addEventListener('click', () => dispatch(state.paused ? 'resume' : 'pause'));

  document.documentElement.classList.add('js');
  const menuButton = byId('menu-toggle');
  const navigation = byId('site-navigation');
  function closeMenu() {
    if (!menuButton || !navigation) return;
    menuButton.setAttribute('aria-expanded', 'false');
    navigation.classList.remove('is-open');
  }
  if (menuButton && navigation) {
    menuButton.addEventListener('click', () => {
      const open = menuButton.getAttribute('aria-expanded') !== 'true';
      menuButton.setAttribute('aria-expanded', String(open));
      navigation.classList.toggle('is-open', open);
    });
    navigation.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && menuButton.getAttribute('aria-expanded') === 'true') {
        closeMenu();
        menuButton.focus();
      }
    });
  }

  window.PantryDemo = Object.freeze({
    initialState,
    reduce,
    getState: () => copyState(state)
  });
  render();
}());
