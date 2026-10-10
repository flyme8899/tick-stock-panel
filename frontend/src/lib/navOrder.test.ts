// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { migrateNavHidden, migrateNavOrder, SECTOR_ANALYSIS_PATH } from './navOrder'

describe('migrateNavOrder', () => {
  it('keeps an empty or already migrated order', () => {
    expect(migrateNavOrder([])).toEqual([])
    expect(migrateNavOrder(['/limit-ladder', SECTOR_ANALYSIS_PATH, '/financials'])).toEqual([
      '/limit-ladder', SECTOR_ANALYSIS_PATH, '/financials',
    ])
  })

  it('replaces a single legacy id in place', () => {
    expect(migrateNavOrder(['/watchlist', '/concept-analysis', '/data'])).toEqual([
      '/watchlist', SECTOR_ANALYSIS_PATH, '/data',
    ])
    expect(migrateNavOrder(['/industry-analysis', '/monitor'])).toEqual([
      SECTOR_ANALYSIS_PATH, '/monitor',
    ])
  })

  it('keeps the earlier of the two legacy slots and drops the later one', () => {
    expect(migrateNavOrder([
      '/limit-ladder', '/industry-analysis', '/financials', '/concept-analysis', '/monitor',
    ])).toEqual([
      '/limit-ladder', SECTOR_ANALYSIS_PATH, '/financials', '/monitor',
    ])
    expect(migrateNavOrder([
      '/concept-analysis', '/industry-analysis',
    ])).toEqual([SECTOR_ANALYSIS_PATH])
  })

  it('collapses a mix of the new id and legacy ids to one entry at the first slot', () => {
    expect(migrateNavOrder([
      '/data', SECTOR_ANALYSIS_PATH, '/concept-analysis', '/industry-analysis',
    ])).toEqual(['/data', SECTOR_ANALYSIS_PATH])
  })

  it('is idempotent', () => {
    const once = migrateNavOrder(['/concept-analysis', '/review', '/industry-analysis'])
    expect(migrateNavOrder(once)).toEqual(once)
  })
})

describe('migrateNavHidden', () => {
  it('drops a single legacy hide so the merged page stays available', () => {
    expect(migrateNavHidden(['/concept-analysis'])).toEqual([])
    expect(migrateNavHidden(['/monitor', '/industry-analysis'])).toEqual(['/monitor'])
  })

  it('hides the merged page when both legacy pages were hidden', () => {
    expect(migrateNavHidden(['/concept-analysis', '/industry-analysis'])).toEqual([
      SECTOR_ANALYSIS_PATH,
    ])
    expect(migrateNavHidden(['/monitor', '/industry-analysis', '/lots', '/concept-analysis'])).toEqual([
      '/monitor', SECTOR_ANALYSIS_PATH, '/lots',
    ])
  })

  it('keeps an explicit hide of the new page and strips leftover legacy ids', () => {
    expect(migrateNavHidden([SECTOR_ANALYSIS_PATH, '/concept-analysis'])).toEqual([
      SECTOR_ANALYSIS_PATH,
    ])
    expect(migrateNavHidden([])).toEqual([])
  })
})
