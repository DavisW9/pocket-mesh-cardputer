// Constrain every IP listener opened by the bundled daemon to this host.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <sys/socket.h>

int bind(int fd, __CONST_SOCKADDR_ARG address, socklen_t size)
{
    int (*real_bind)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "bind");
    if (!real_bind) {
        errno = ENOSYS;
        return -1;
    }
    const struct sockaddr *addr = address.__sockaddr__;
    if (addr && size >= sizeof(struct sockaddr_in) && addr->sa_family == AF_INET) {
        struct sockaddr_in local = *(const struct sockaddr_in *)addr;
        local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        return real_bind(fd, (const struct sockaddr *)&local, sizeof(local));
    }
    if (addr && size >= sizeof(struct sockaddr_in6) && addr->sa_family == AF_INET6) {
        struct sockaddr_in6 local = *(const struct sockaddr_in6 *)addr;
        local.sin6_addr = in6addr_loopback;
        local.sin6_scope_id = 0;
        return real_bind(fd, (const struct sockaddr *)&local, sizeof(local));
    }
    return real_bind(fd, addr, size);
}
