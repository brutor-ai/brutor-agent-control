/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The entry point of the Community console: the
 * console with no extensions. It imports nothing from another distribution;
 * the Enterprise console has an entry point of its own.
 */

import { mountConsole } from './app/mount';
import './styles/globals.css';

mountConsole();
