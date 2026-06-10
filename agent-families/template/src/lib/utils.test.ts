import { expect, test } from 'vitest'

import { cn } from './utils'

test('cn merges conflicting tailwind classes (last wins)', () => {
  expect(cn('p-2', 'p-4')).toBe('p-4')
})

test('cn drops falsy values', () => {
  expect(cn('a', false, undefined, 'c')).toBe('a c')
})
