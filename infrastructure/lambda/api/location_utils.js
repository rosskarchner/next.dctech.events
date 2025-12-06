/**
 * Location utility functions for extracting and normalizing location data
 */

/**
 * Extract city and state from an address string
 * @param {string} address - The address string to parse
 * @returns {{city: string|null, state: string|null}} Object with city and state
 */
function extractLocationInfo(address) {
  if (!address) {
    return { city: null, state: null };
  }

  // Pattern to match: [anything], City, State [ZIP] [, USA]
  const pattern = /([^,]+),\s*([^,]+),\s*([A-Z]{2})(?:\s+\d{5})?(?:,\s*USA)?$/i;
  const match = address.match(pattern);

  if (match) {
    const [, , city, state] = match;
    const cleanCity = city.trim();
    const cleanState = state.toUpperCase();

    // Skip if "city" looks like a street address or building
    const streetPattern = /\d+\s+\w+\s+(st|street|ave|avenue|blvd|boulevard|dr|drive|rd|road|ct|court|ln|lane|way|pl|place)/i;
    const buildingPattern = /suite|floor|#|room|building/i;
    
    if (streetPattern.test(cleanCity) || buildingPattern.test(cleanCity) || cleanCity.split(' ').length > 4) {
      return { city: null, state: null };
    }

    // Handle special cases
    let finalCity = cleanCity;
    if (cleanState === 'DC') {
      finalCity = 'Washington';
    }

    return { city: finalCity, state: cleanState };
  }

  return { city: null, state: null };
}

/**
 * Get region name from state abbreviation
 * @param {string} state - State abbreviation
 * @returns {string} Full region name
 */
function getRegionName(state) {
  const regions = {
    'DC': 'Washington DC',
    'VA': 'Virginia',
    'MD': 'Maryland'
  };
  return regions[state] || state;
}

/**
 * Normalize addresses by removing redundant information
 * @param {string} address - The address string to normalize
 * @returns {string} Normalized address string
 */
function normalizeAddress(address) {
  if (!address || typeof address !== 'string') {
    return address;
  }

  // Split by commas and clean each part
  let parts = address.split(',').map(part => part.trim()).filter(part => part);

  if (parts.length <= 1) {
    return address;
  }

  // Remove consecutive duplicates (case-insensitive)
  const normalizedParts = [];
  for (const part of parts) {
    if (normalizedParts.length === 0 || 
        part.toLowerCase() !== normalizedParts[normalizedParts.length - 1].toLowerCase()) {
      normalizedParts.push(part);
    }
  }

  // Remove state/city duplicates at the end
  // Common pattern: "City, State, City, State" -> "City, State"
  if (normalizedParts.length >= 4) {
    const last = normalizedParts[normalizedParts.length - 1].toLowerCase();
    const secondLast = normalizedParts[normalizedParts.length - 2].toLowerCase();
    const thirdLast = normalizedParts[normalizedParts.length - 3].toLowerCase();
    const fourthLast = normalizedParts[normalizedParts.length - 4].toLowerCase();

    if (last === thirdLast && secondLast === fourthLast) {
      normalizedParts.splice(normalizedParts.length - 2, 2);
    }
  }

  // Remove exact duplicates that aren't consecutive (case-insensitive)
  const seen = new Set();
  const finalParts = [];
  for (const part of normalizedParts) {
    const partLower = part.toLowerCase();
    if (!seen.has(partLower)) {
      seen.add(partLower);
      finalParts.push(part);
    }
  }

  // Handle common state abbreviation duplicates (e.g., "Arlington, VA, VA")
  if (finalParts.length >= 2) {
    const lastPart = finalParts[finalParts.length - 1].trim();
    const secondLast = finalParts[finalParts.length - 2].trim();

    const states = new Set(['VA', 'DC', 'MD', 'WV']);

    if (states.has(lastPart) && 
        (secondLast.includes(lastPart) || secondLast.endsWith(` ${lastPart}`))) {
      finalParts.pop();
    }
  }

  return finalParts.join(', ');
}

module.exports = {
  extractLocationInfo,
  getRegionName,
  normalizeAddress
};
