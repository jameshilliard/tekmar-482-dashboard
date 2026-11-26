// Keep an immutable view of the buffer while the operator reads older traffic.
// Rows are keyed by event object, not sequence: a restarted server can reuse IDs.
export class TraceHistory {
  constructor(scroller, button, drawRow, drawLatest) {
    this.scroller = scroller;
    this.body = scroller.querySelector("tbody");
    this.button = button;
    this.drawRow = drawRow;
    this.drawLatest = drawLatest;
    this.cache = new WeakMap();
    this.events = [];
    this.held = null;
    this.limit = 120;
    scroller.addEventListener("scroll", () => this.onScroll(), {
      passive: true,
    });
    button.addEventListener("click", () => this.follow());
  }

  onScroll() {
    const box = this.scroller;
    if (box.scrollTop <= 1) {
      if (this.held) this.follow();
      return;
    }
    if (!this.held) {
      this.held = this.drawn || [];
      this.button.disabled = false;
      this.button.textContent = "Jump to live";
    }
    if (
      this.limit < this.held.length &&
      box.scrollHeight - box.scrollTop - box.clientHeight < box.clientHeight / 2
    ) {
      this.limit += 120;
      this.draw(this.held);
    }
  }

  follow() {
    this.held = null;
    this.limit = 120;
    this.scroller.scrollTop = 0;
    this.button.disabled = true;
    this.button.textContent = "Following live";
    this.draw(this.events);
  }

  update(events, filter) {
    this.events = events;
    if (filter !== this.filter) {
      this.filter = filter;
      this.follow();
    } else if (this.scroller.scrollTop > 1 || this.held) {
      this.onScroll();
    } else {
      this.draw(events);
    }
  }

  draw(events) {
    this.drawn = events;
    const newest = events.findLast((e) => e.hex);
    if (this.newest !== newest) {
      this.newest = newest;
      this.drawLatest(newest);
    }
    const wanted = events.slice(-this.limit).reverse();
    if (!wanted.length) {
      if (this.body.querySelector(".empty")) return;
      const row = document.createElement("tr"),
        cell = row.insertCell();
      cell.colSpan = 3;
      cell.className = "empty";
      cell.textContent = "Waiting for matching messages";
      this.body.replaceChildren(row);
      return;
    }
    let next = this.body.firstElementChild;
    for (const event of wanted) {
      let row = this.cache.get(event);
      if (!row) {
        row = this.drawRow(event);
        this.cache.set(event, row);
      }
      if (row === next) next = next.nextElementSibling;
      else this.body.insertBefore(row, next);
    }
    while (next) {
      const old = next;
      next = next.nextElementSibling;
      old.remove();
    }
  }
}
