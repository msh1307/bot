// Reject unusable summary results before Pi can commit a compaction entry.
// Ordinary conversation streams never pass through this guard.
export function guardSummaryStream(source, model, lazyStream) {
  return lazyStream(model, async () => ({
    async *[Symbol.asyncIterator]() {
      for await (const event of source) {
        if (event.type === 'done' || event.type === 'error') {
          const message = event.type === 'done' ? event.message : event.error;
          let errorMessage;
          if (message.stopReason === 'aborted')
            errorMessage = 'Compaction summary was aborted; previous context was retained.';
          else if (event.type === 'done' && !['error', 'length'].includes(message.stopReason)
              && !message.content.some(block => block.type === 'text' && block.text?.trim()))
            errorMessage = 'Compaction summary contained no final text; previous context was retained.';
          if (errorMessage) {
            yield { type: 'error', reason: 'error', error: { ...message, stopReason: 'error', errorMessage } };
            return;
          }
        }
        yield event;
      }
    },
  }));
}
