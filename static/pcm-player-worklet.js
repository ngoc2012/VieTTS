class PCMPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunks = [];
    this.offset = 0;
    this.streamDone = false;
    this.port.onmessage = (e) => {
      if (e.data && e.data.done) {
        this.streamDone = true;
      } else {
        this.chunks.push(e.data);
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0][0];
    let written = 0;

    while (written < output.length && this.chunks.length > 0) {
      const chunk = this.chunks[0];
      const available = chunk.length - this.offset;
      const needed = output.length - written;
      const toCopy = Math.min(available, needed);

      for (let i = 0; i < toCopy; i++) {
        output[written + i] = chunk[this.offset + i];
      }

      written += toCopy;
      this.offset += toCopy;

      if (this.offset >= chunk.length) {
        this.chunks.shift();
        this.offset = 0;
      }
    }

    // Fill remaining with silence
    for (let i = written; i < output.length; i++) {
      output[i] = 0;
    }

    // Stream finished and buffer drained — signal done
    if (this.streamDone && this.chunks.length === 0) {
      this.port.postMessage({ finished: true });
      return false;
    }

    return true;
  }
}

registerProcessor('pcm-player', PCMPlayerProcessor);
