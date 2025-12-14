#!/usr/bin/env node
"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
const cdk = __importStar(require("aws-cdk-lib"));
const infrastructure_stack_1 = require("../lib/infrastructure-stack");
const app = new cdk.App();
new infrastructure_stack_1.InfrastructureStack(app, 'InfrastructureStack', {
    // Use the account and region from the current AWS CLI configuration
    // Required for Route 53 hosted zone lookup and ACM certificate
    env: {
        account: process.env.CDK_DEFAULT_ACCOUNT,
        region: process.env.CDK_DEFAULT_REGION
    },
    synthesizer: new cdk.DefaultStackSynthesizer({
        qualifier: 'dctech1',
    }),
});
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoiaW5mcmFzdHJ1Y3R1cmUuanMiLCJzb3VyY2VSb290IjoiIiwic291cmNlcyI6WyJpbmZyYXN0cnVjdHVyZS50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7QUFDQSxpREFBbUM7QUFDbkMsc0VBQWtFO0FBRWxFLE1BQU0sR0FBRyxHQUFHLElBQUksR0FBRyxDQUFDLEdBQUcsRUFBRSxDQUFDO0FBQzFCLElBQUksMENBQW1CLENBQUMsR0FBRyxFQUFFLHFCQUFxQixFQUFFO0lBQ2xELG9FQUFvRTtJQUNwRSwrREFBK0Q7SUFDL0QsR0FBRyxFQUFFO1FBQ0gsT0FBTyxFQUFFLE9BQU8sQ0FBQyxHQUFHLENBQUMsbUJBQW1CO1FBQ3hDLE1BQU0sRUFBRSxPQUFPLENBQUMsR0FBRyxDQUFDLGtCQUFrQjtLQUN2QztJQUNELFdBQVcsRUFBRSxJQUFJLEdBQUcsQ0FBQyx1QkFBdUIsQ0FBQztRQUMzQyxTQUFTLEVBQUUsU0FBUztLQUNyQixDQUFDO0NBQ0gsQ0FBQyxDQUFDIiwic291cmNlc0NvbnRlbnQiOlsiIyEvdXNyL2Jpbi9lbnYgbm9kZVxuaW1wb3J0ICogYXMgY2RrIGZyb20gJ2F3cy1jZGstbGliJztcbmltcG9ydCB7IEluZnJhc3RydWN0dXJlU3RhY2sgfSBmcm9tICcuLi9saWIvaW5mcmFzdHJ1Y3R1cmUtc3RhY2snO1xuXG5jb25zdCBhcHAgPSBuZXcgY2RrLkFwcCgpO1xubmV3IEluZnJhc3RydWN0dXJlU3RhY2soYXBwLCAnSW5mcmFzdHJ1Y3R1cmVTdGFjaycsIHtcbiAgLy8gVXNlIHRoZSBhY2NvdW50IGFuZCByZWdpb24gZnJvbSB0aGUgY3VycmVudCBBV1MgQ0xJIGNvbmZpZ3VyYXRpb25cbiAgLy8gUmVxdWlyZWQgZm9yIFJvdXRlIDUzIGhvc3RlZCB6b25lIGxvb2t1cCBhbmQgQUNNIGNlcnRpZmljYXRlXG4gIGVudjoge1xuICAgIGFjY291bnQ6IHByb2Nlc3MuZW52LkNES19ERUZBVUxUX0FDQ09VTlQsXG4gICAgcmVnaW9uOiBwcm9jZXNzLmVudi5DREtfREVGQVVMVF9SRUdJT05cbiAgfSxcbiAgc3ludGhlc2l6ZXI6IG5ldyBjZGsuRGVmYXVsdFN0YWNrU3ludGhlc2l6ZXIoe1xuICAgIHF1YWxpZmllcjogJ2RjdGVjaDEnLFxuICB9KSxcbn0pO1xuIl19