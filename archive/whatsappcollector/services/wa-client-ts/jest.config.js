/** @type {import('ts-jest').JestConfigWithTsJest} */
module.exports = {
    testEnvironment: 'node',
    rootDir: '../../',
    testMatch: ['<rootDir>/tests/ts_tests/**/*.test.ts'],
    moduleDirectories: ['node_modules', '<rootDir>/services/wa-client-ts/node_modules'],
    moduleNameMapper: {
        '^@/(.*)$': '<rootDir>/services/wa-client-ts/src/$1',
        '^@whiskeysockets/baileys$': '<rootDir>/tests/ts_tests/__mocks__/baileys.ts'
    },
    transform: {
        '^.+\\.tsx?$': ['<rootDir>/services/wa-client-ts/node_modules/ts-jest', {
            tsconfig: '<rootDir>/services/wa-client-ts/tsconfig.json',
            diagnostics: false
        }]
    }
};
