'use strict';

import clone from './structured-clone/esm/index.js';

function defineMissing(target, name, value) {
  if (typeof target[name] !== 'function') {
    Object.defineProperty(target, name, { configurable: true, writable: true, value });
  }
}

defineMissing(Object, 'hasOwn', function hasOwn(value, property) {
  if (value == null) throw new TypeError('Cannot convert undefined or null to object');
  return Object.prototype.hasOwnProperty.call(value, property);
});

function relativeIndex(index, length) {
  const integer = Math.trunc(Number(index) || 0);
  return integer < 0 ? length + integer : integer;
}

defineMissing(Array.prototype, 'at', function at(index) {
  if (this == null) throw new TypeError('Array.prototype.at called on null or undefined');
  const value = Object(this);
  const length = Math.min(Math.max(Math.trunc(Number(value.length) || 0), 0), Number.MAX_SAFE_INTEGER);
  const position = relativeIndex(index, length);
  return position >= 0 && position < length ? value[position] : undefined;
});

defineMissing(String.prototype, 'at', function at(index) {
  if (this == null) throw new TypeError('String.prototype.at called on null or undefined');
  const value = String(this);
  const position = relativeIndex(index, value.length);
  return position >= 0 && position < value.length ? value.charAt(position) : undefined;
});

defineMissing(String.prototype, 'replaceAll', function replaceAll(searchValue, replaceValue) {
  if (this == null) throw new TypeError('String.prototype.replaceAll called on null or undefined');
  if (searchValue != null) {
    const match = searchValue[Symbol.match];
    const isRegExp = match === undefined
      ? Object.prototype.toString.call(searchValue) === '[object RegExp]'
      : Boolean(match);
    if (isRegExp) {
      const flags = searchValue.flags;
      if (flags == null || !String(flags).includes('g')) {
        throw new TypeError('String.prototype.replaceAll requires a global RegExp');
      }
    }
    const replacer = searchValue[Symbol.replace];
    if (replacer != null) return replacer.call(searchValue, this, replaceValue);
  }
  const search = String(searchValue).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return String(this).replace(new RegExp(search, 'g'), replaceValue);
});

defineMissing(globalThis, 'structuredClone', function structuredClone(value, options) {
  if (options != null && options.transfer != null) {
    for (const transferable of options.transfer) {
      throw new DOMException('Transfer is not supported by this structuredClone fallback', 'DataCloneError');
    }
  }
  try {
    return clone(value);
  } catch (error) {
    if (error instanceof TypeError && /^unable to serialize (function|symbol)$/.test(error.message)) {
      throw new DOMException(error.message, 'DataCloneError');
    }
    throw error;
  }
});

if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
  defineMissing(globalThis.crypto, 'randomUUID', function randomUUID() {
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0'));
    return hex.slice(0, 4).join('') + '-' + hex.slice(4, 6).join('') + '-'
      + hex.slice(6, 8).join('') + '-' + hex.slice(8, 10).join('') + '-' + hex.slice(10).join('');
  });
}
